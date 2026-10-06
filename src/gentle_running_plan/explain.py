"""解释链：把每次处方变化与风险门槛、实际执行串成可读证据。

输出按时间排列的"事实条目"，每一步都引用事件 id、规则编号和课次，
回答三个问题：
1. 这节课为什么这样安排（授权门槛 -> 计划版本 -> 课次）；
2. 为什么被暂停（执行事实 -> 红旗/越限 -> 暂停）；
3. 调整从哪节课生效、依据是什么（暂停/复核 -> 新版本生效点）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .events import EventType
from .ids import participant_stream, plan_stream, safety_stream
from .plan import PlanService
from .store import EventStore


@dataclass(frozen=True)
class ExplanationItem:
    order: int
    at: str
    kind: str
    title: str
    detail: str
    event_id: str
    refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "at": self.at,
            "kind": self.kind,
            "title": self.title,
            "detail": self.detail,
            "event_id": self.event_id,
            "refs": list(self.refs),
        }


class ExplanationService:
    def __init__(self, store: EventStore) -> None:
        self.store = store
        self.plans = PlanService(store)

    def timeline(self, participant_id: str) -> list[ExplanationItem]:
        events = (
            self.store.stream(participant_stream(participant_id))
            + self.store.stream(plan_stream(participant_id))
            + self.store.stream(safety_stream(participant_id))
            + [
                e for e in self.store.all_events()
                if e.aggregate_type == "training_session"
                and e.payload.get("participant_id") == participant_id
            ]
        )
        items: list[ExplanationItem] = []
        order = 0
        for event in sorted(events, key=lambda e: (e.occurred_at, e.event_type)):
            builder = _BUILDERS.get(event.event_type)
            if builder is None:
                continue
            order += 1
            items.append(builder(order, event))
        return items

    def session_chain(self, participant_id: str, seq: int) -> dict[str, Any]:
        """解释某一节课：适用版本、门槛、执行情况、是否被暂停影响。"""
        spec = self.plans.effective_session(participant_id, seq)
        snapshot = self.plans.effective_authorization(participant_id, seq)
        revisions = self.plans.revisions(participant_id)
        governing = [
            rev for rev in revisions
            if rev.effective_from_seq <= seq and rev.session(seq) is not None
        ][-1]
        logged = [
            e for e in self.store.query(event_type=EventType.SESSION_LOGGED)
            if e.payload.get("participant_id") == participant_id
            and e.payload.get("session_seq") == seq
        ]
        from .clock import parse_time

        due = parse_time(spec.scheduled_at)
        return {
            "session_seq": seq,
            "scheduled_at": spec.scheduled_at,
            "plan_version": governing.plan_version,
            "effective_from_seq": governing.effective_from_seq,
            "revision_reason": governing.reason,
            "thresholds": {
                k: snapshot.get(k)
                for k in (
                    "max_heart_rate", "max_rpe", "max_session_minutes",
                    "max_jog_bout_seconds", "walk_jog_ratio", "allowed_modes",
                )
            },
            "rule_ids": snapshot.get("rule_ids", []),
            "spec": spec.to_dict(),
            "execution": [
                {
                    "event_id": e.event_id,
                    "observed_at": e.payload.get("observed_at"),
                    "readings": e.payload.get("readings"),
                    "symptoms": e.payload.get("symptoms"),
                    "plan_version": e.payload.get("plan_version"),
                }
                for e in logged
            ],
            "paused_when_due": _was_paused_at(
                self.store, participant_id, due
            ),
        }

    def change_report(self, participant_id: str) -> list[dict[str, Any]]:
        """列出每次处方变化及生效课次与依据。"""
        report: list[dict[str, Any]] = []
        for rev in self.plans.revisions(participant_id):
            report.append({
                "plan_version": rev.plan_version,
                "effective_from_seq": rev.effective_from_seq,
                "issued_at": rev.issued_at.isoformat(),
                "reason": rev.reason,
                "threshold_rule_ids": rev.authorization_snapshot.get("rule_ids", []),
                "future_sessions_only": True,
            })
        return report


def _was_paused_at(
    store: EventStore, participant_id: str, moment: Any
) -> bool:
    """按时间线走到 moment：最近一个暂停/恢复事件决定当时是否暂停。"""
    if isinstance(moment, str):
        from .clock import parse_time

        moment = parse_time(moment)
    transitions: list[tuple[Any, str]] = [
        (e.occurred_at, "paused")
        for e in store.stream(safety_stream(participant_id))
        if e.event_type == EventType.SAFETY_PAUSED
    ]
    transitions.extend(
        (e.occurred_at, "resumed")
        for e in store.stream(plan_stream(participant_id))
        if e.event_type == EventType.PLAN_RESUMED
    )
    state = False
    for at, transition in sorted(transitions, key=lambda item: item[0]):
        if at > moment:
            break
        state = transition == "paused"
    return state


def _screening_item(order: int, event: Any) -> ExplanationItem:
    auth = event.payload.get("authorization", {})
    rules = "、".join(b.get("rule_id", "?") for b in auth.get("basis", []))
    return ExplanationItem(
        order, event.occurred_at.isoformat(), "screening",
        event.summary,
        f"授权心率上限 {auth.get('max_heart_rate')} 次/分、"
        f"RPE≤{auth.get('max_rpe')}、单次≤{auth.get('max_session_minutes')} 分钟；"
        f"依据规则 {rules}",
        event.event_id,
        tuple(b.get("rule_id", "") for b in auth.get("basis", [])),
    )


def _review_item(order: int, event: Any) -> ExplanationItem:
    return ExplanationItem(
        order, event.occurred_at.isoformat(), "review",
        event.summary,
        f"复核人 {event.payload.get('reviewer_id')} 结论 "
        f"{event.payload.get('verdict')}：{event.payload.get('note', '')}",
        event.event_id,
        ("professional_review",),
    )


def _plan_published_item(order: int, event: Any) -> ExplanationItem:
    sessions = event.payload.get("sessions", [])
    return ExplanationItem(
        order, event.occurred_at.isoformat(), "plan_published",
        event.summary,
        f"v{event.payload.get('plan_version')} 发布，共 {len(sessions)} 课，"
        f"规则门槛 {event.payload.get('authorization_snapshot', {}).get('rule_ids')}",
        event.event_id,
        tuple(event.payload.get("authorization_snapshot", {}).get("rule_ids", [])),
    )


def _plan_adjusted_item(order: int, event: Any) -> ExplanationItem:
    return ExplanationItem(
        order, event.occurred_at.isoformat(), "plan_adjusted",
        event.summary,
        f"v{event.payload.get('plan_version')}（取代 v"
        f"{event.payload.get('supersedes_version')}）自第 "
        f"{event.payload.get('effective_from_seq')} 课起生效；"
        f"此前已完成课次不变。原因：{event.payload.get('reason')}",
        event.event_id,
        tuple(event.payload.get("authorization_snapshot", {}).get("rule_ids", [])),
    )


def _session_item(order: int, event: Any) -> ExplanationItem:
    readings = event.payload.get("readings", {})
    return ExplanationItem(
        order, event.occurred_at.isoformat(), "session_logged",
        event.summary,
        f"体征 {readings}；症状 {event.payload.get('symptoms', [])}；"
        f"对应计划 v{event.payload.get('plan_version')} 第 "
        f"{event.payload.get('session_seq')} 课",
        event.event_id,
        (f"plan-v{event.payload.get('plan_version')}",),
    )


def _conflict_item(order: int, event: Any) -> ExplanationItem:
    return ExplanationItem(
        order, event.occurred_at.isoformat(), "report_conflicted",
        event.summary,
        f"类型 {event.payload.get('conflict_type')}；"
        f"冲突指标 {[c.get('metric') for c in event.payload.get('conflicts', [])]}；"
        "保留先到记录，等待人工复核，未取平均",
        event.event_id,
        (f"kept:{event.payload.get('kept_event_id')}",),
    )


def _pause_item(order: int, event: Any) -> ExplanationItem:
    breaches = event.payload.get("breaches", [])
    flags = [f.get("label") for f in event.payload.get("red_flags", [])]
    refs = tuple(b.get("rule", "") for b in breaches) + (
        f"source:{event.payload.get('source_event_id')}",
    )
    return ExplanationItem(
        order, event.occurred_at.isoformat(), "safety_paused",
        event.summary,
        f"触发：{event.payload.get('trigger')}；红旗 {flags}；"
        f"越限 {[b.get('message') for b in breaches]}；"
        "后续所有课次立即暂停，仅新的专业复核可恢复",
        event.event_id,
        refs,
    )


def _resume_item(order: int, event: Any) -> ExplanationItem:
    return ExplanationItem(
        order, event.occurred_at.isoformat(), "plan_resumed",
        event.summary,
        f"依据复核 {event.payload.get('review_event_id')} 解除暂停 "
        f"{event.payload.get('pause_event_id')}，从后续课次恢复",
        event.event_id,
        (f"review:{event.payload.get('review_event_id')}",),
    )


_BUILDERS = {
    EventType.SCREENING_APPROVED: _screening_item,
    EventType.SCREENING_SUBMITTED: _screening_item,
    EventType.REVIEW_RECORDED: _review_item,
    EventType.REVIEW_CLEARED: _review_item,
    EventType.PLAN_PUBLISHED: _plan_published_item,
    EventType.PLAN_ADJUSTED: _plan_adjusted_item,
    EventType.SESSION_LOGGED: _session_item,
    EventType.REPORT_CONFLICTED: _conflict_item,
    EventType.SAFETY_PAUSED: _pause_item,
    EventType.PLAN_RESUMED: _resume_item,
}
