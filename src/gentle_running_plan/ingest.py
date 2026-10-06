"""训练记录接入：离线晚到、重复上报与数值冲突。

规则：
- 设备/表格携带幂等键 ``report_id``：同一键重放保持一次有效记录。
- 同一参与者同一课次（无课次时按观察日）的不同上报，关键数值一致时
  保留先到的一条；数值冲突写入 REPORT_CONFLICTED 转人工复核，绝不取平均。
- 原始体征随事件原样留档；计划改写不影响已经成立的训练事实。
- 红旗症状或超授权负荷：SESSION_LOGGED 与 SAFETY_PAUSED 同批原子提交，
  暂停立即对后续课次生效。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping, Sequence

from .clock import Clock, SystemClock, parse_time
from .events import AggregateType, Event, EventType, make_event
from .ids import session_stream
from .plan import PlanService
from .safety import (
    RED_FLAG_CATALOG,
    SafetyService,
    ThresholdBreach,
    evaluate_breaches,
)
from .store import EventStore


class IngestStatus(StrEnum):
    LOGGED = "logged"                    # 新有效记录
    DUPLICATE_EXACT = "duplicate_exact"  # 同一 report_id 重放
    DUPLICATE_SLOT = "duplicate_slot"    # 同学段同值重复，保留先到记录
    CONFLICTED = "conflicted"            # 数值冲突，转复核
    REJECTED_PAUSED = "rejected_paused"  # 暂停后发生的活动，转复核不计完成


# 同学段两次上报视为一致的容差（容差内保留先到值，不做平均）
TOLERANCES = {
    "peak_heart_rate": 2,   # 次/分
    "avg_heart_rate": 2,
    "resting_heart_rate": 2,
    "duration_minutes": 1,  # 分钟
    "distance_km": 0.1,
}
COMPARED_METRICS = (
    "peak_heart_rate", "avg_heart_rate", "resting_heart_rate",
    "duration_minutes", "distance_km", "rpe",
)


@dataclass(frozen=True)
class IngestResult:
    status: IngestStatus
    event: Event | None
    session_seq: int | None
    conflicts: tuple[dict[str, Any], ...] = ()
    paused: bool = False
    pause_event: Event | None = None
    message: str = ""


class IngestService:
    def __init__(
        self,
        store: EventStore,
        plans: PlanService,
        safety: SafetyService,
        clock: Clock | None = None,
    ) -> None:
        self.store = store
        self.plans = plans
        self.safety = safety
        self.clock = clock or SystemClock()

    def log_report(
        self,
        participant_id: str,
        report: Mapping[str, Any],
        *,
        received_at: datetime | str | None = None,
    ) -> IngestResult:
        report = dict(report)
        report_id = str(report.get("report_id", "")).strip()
        if not report_id:
            raise ValueError("上报必须携带设备/表格侧的 report_id")
        received_at = parse_time(received_at) if isinstance(received_at, str) \
            else (received_at or self.clock.now())
        observed_at = parse_time(str(report["observed_at"]))
        seq_value = report.get("session_seq")
        session_seq = int(seq_value) if seq_value is not None else None
        slot = _slot_key(participant_id, session_seq, observed_at)

        # 1) 幂等键重放：晚到/重试的同一条上报直接返回原事件
        existing = self._find_by_report(participant_id, report_id)
        if existing is not None:
            return IngestResult(
                IngestStatus.DUPLICATE_EXACT, existing, session_seq,
                message=f"report_id {report_id} 已登记，保持一次有效记录",
            )

        # 2) 同学段已有有效记录：比较关键数值
        kept = self._find_slot_record(participant_id, slot)
        if kept is not None:
            first = {
                **kept.payload.get("readings", {}),
                "symptoms": kept.payload.get("symptoms", []),
            }
            second = {
                **report.get("readings", {}),
                "symptoms": report.get("symptoms", []),
            }
            conflicts = _compare_readings(first, second)
            if conflicts:
                event = self._record_conflict(
                    participant_id, slot, session_seq, report_id,
                    observed_at, received_at, kept, report, conflicts,
                )
                return IngestResult(
                    IngestStatus.CONFLICTED, event, session_seq,
                    conflicts=tuple(conflicts),
                    message="同一课次数值冲突，已转人工复核，未取平均",
                )
            return IngestResult(
                IngestStatus.DUPLICATE_SLOT, kept, session_seq,
                message="同一课次重复上报且数值一致，保留先到记录",
            )

        # 3) 暂停期间发生的活动：原始体征仍留档，但不计入计划完成，转复核
        if SafetyService.is_paused(self.store, participant_id):
            event = self._record_conflict(
                participant_id, slot, session_seq, report_id,
                observed_at, received_at, None, report,
                [{"metric": "paused_window",
                  "message": "活动发生在安全暂停期内，需复核后认定"}],
                conflict_type="paused_window_activity",
            )
            return IngestResult(
                IngestStatus.REJECTED_PAUSED, event, session_seq,
                message="暂停期内的活动已留档并转复核，不计训练完成",
            )

        # 4) 新有效记录：必要时同批写入安全暂停
        return self._accept(
            participant_id, report, report_id, slot, session_seq,
            observed_at, received_at,
        )

    # -- 内部 ---------------------------------------------------------------

    def _accept(
        self,
        participant_id: str,
        report: Mapping[str, Any],
        report_id: str,
        slot: str,
        session_seq: int | None,
        observed_at: datetime,
        received_at: datetime,
    ) -> IngestResult:
        readings = dict(report.get("readings", {}))
        symptoms = list(dict.fromkeys(report.get("symptoms", [])))
        unknown_symptoms = [s for s in symptoms if s not in RED_FLAG_CATALOG]
        if unknown_symptoms:
            raise ValueError(f"未登记的红旗症状代码: {unknown_symptoms}")

        auth_snapshot: Mapping[str, Any] = {}
        plan_version: int | None = None
        if session_seq is not None:
            auth_snapshot = self.plans.effective_authorization(
                participant_id, session_seq
            )
            plan_version = self._effective_plan_version(participant_id, session_seq)
        breaches = evaluate_breaches(readings, auth_snapshot)
        red_flags = [s for s in symptoms if s in RED_FLAG_CATALOG]
        must_pause = bool(red_flags or breaches)

        stream_id = session_stream(participant_id, session_seq) \
            if session_seq is not None else f"session:{participant_id}:adhoc"
        logged = make_event(
            EventType.SESSION_LOGGED,
            AggregateType.TRAINING_SESSION,
            stream_id,
            observed_at,
            version=self.store.version_of(stream_id) + 1,
            summary=(
                f"第 {session_seq} 课训练记录已登记"
                if session_seq is not None else "自行活动记录已登记"
            ),
            payload={
                "participant_id": participant_id,
                "session_seq": session_seq,
                "slot": slot,
                "report_id": report_id,
                "source": str(report.get("source", "device")),
                "observed_at": observed_at.isoformat(),
                "received_at": received_at.isoformat(),
                "late": received_at > observed_at,
                "readings": readings,
                "symptoms": symptoms,
                "note": str(report.get("note", "")),
                "plan_version": plan_version,
                "authorization_snapshot": dict(auth_snapshot),
                "counts_toward_plan": session_seq is not None,
            },
        )

        if not must_pause:
            event = self.store.append(logged)
            return IngestResult(IngestStatus.LOGGED, event, session_seq)

        reason = _pause_reason(red_flags, breaches)
        pause = make_event(
            EventType.SAFETY_PAUSED,
            AggregateType.SAFETY_REPORT,
            f"safety:{participant_id}",
            self.clock.now(),
            version=self.store.version_of(f"safety:{participant_id}") + 1,
            summary=f"安全暂停：{reason}",
            payload={
                "participant_id": participant_id,
                "reason": reason,
                "trigger": "red_flag" if red_flags else "threshold_breach",
                "source_event_id": logged.event_id,
                "red_flags": [
                    {"code": f, "label": RED_FLAG_CATALOG[f]} for f in red_flags
                ],
                "breaches": [b.to_dict() for b in breaches],
                "detail": {"session_seq": session_seq, "slot": slot},
            },
        )
        self.store.append_many([logged, pause])
        return IngestResult(
            IngestStatus.LOGGED, logged, session_seq,
            paused=True, pause_event=pause,
            message="记录已登记并因安全风险立即暂停后续训练",
        )

    def _effective_plan_version(self, participant_id: str, seq: int) -> int:
        applicable = [
            rev for rev in self.plans.revisions(participant_id)
            if rev.effective_from_seq <= seq and rev.session(seq) is not None
        ]
        return applicable[-1].plan_version

    def _record_conflict(
        self,
        participant_id: str,
        slot: str,
        session_seq: int | None,
        report_id: str,
        observed_at: datetime,
        received_at: datetime,
        kept: Event | None,
        report: Mapping[str, Any],
        conflicts: Sequence[dict[str, Any]],
        *,
        conflict_type: str = "value_conflict",
    ) -> Event:
        stream_id = (
            session_stream(participant_id, session_seq)
            if session_seq is not None else f"session:{participant_id}:adhoc"
        )
        event = make_event(
            EventType.REPORT_CONFLICTED,
            AggregateType.TRAINING_SESSION,
            stream_id,
            received_at,
            version=self.store.version_of(stream_id) + 1,
            summary="上报冲突已转人工复核",
            payload={
                "participant_id": participant_id,
                "session_seq": session_seq,
                "slot": slot,
                "conflict_type": conflict_type,
                "report_id": report_id,
                "kept_event_id": kept.event_id if kept else None,
                "observed_at": observed_at.isoformat(),
                "received_at": received_at.isoformat(),
                "raw_report": dict(report),
                "conflicts": list(conflicts),
                "resolution": "pending_review",
            },
        )
        return self.store.append(event)

    def _find_by_report(
        self, participant_id: str, report_id: str
    ) -> Event | None:
        for event in self.store.query(event_type=EventType.SESSION_LOGGED):
            if (event.payload.get("participant_id") == participant_id
                    and event.payload.get("report_id") == report_id):
                return event
        return None

    def _find_slot_record(
        self, participant_id: str, slot: str
    ) -> Event | None:
        for event in self.store.query(event_type=EventType.SESSION_LOGGED):
            if (event.payload.get("participant_id") == participant_id
                    and event.payload.get("slot") == slot):
                return event
        return None


def _slot_key(
    participant_id: str, session_seq: int | None, observed_at: datetime
) -> str:
    if session_seq is not None:
        return f"{participant_id}#seq#{session_seq}"
    return f"{participant_id}#day#{observed_at.date().isoformat()}"


def _compare_readings(
    first: Mapping[str, Any], second: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """逐指标比较；只报告冲突，不产生平均值。"""
    conflicts: list[dict[str, Any]] = []
    for metric in COMPARED_METRICS:
        # 允许设备上报字段子集：只有双方都提供了同一指标才比较
        if metric not in first or metric not in second:
            continue
        a, b = first[metric], second[metric]
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            if a != b:
                conflicts.append({
                    "metric": metric, "kept": a, "incoming": b,
                    "message": "非数值指标不一致",
                })
            continue
        tolerance = TOLERANCES.get(metric, 0)
        if abs(a - b) > tolerance:
            conflicts.append({
                "metric": metric, "kept": a, "incoming": b,
                "tolerance": tolerance,
                "message": f"{metric}: {a} 与 {b} 差值超过容差 {tolerance}",
            })
    # 症状多报不可吞掉：后到者报告了先到记录没有的红旗症状也算冲突
    first_symptoms = set(first.get("symptoms", [])) if isinstance(
        first.get("symptoms"), (set, list, tuple)) else set()
    second_symptoms = set(second.get("symptoms", []))
    extra = second_symptoms - first_symptoms
    if extra:
        conflicts.append({
            "metric": "symptoms",
            "kept": sorted(first_symptoms),
            "incoming": sorted(second_symptoms),
            "message": f"后到上报包含额外症状: {sorted(extra)}",
        })
    return conflicts


def _pause_reason(
    red_flags: Sequence[str], breaches: Sequence[ThresholdBreach]
) -> str:
    parts: list[str] = []
    if red_flags:
        parts.append("红旗症状 " + "、".join(
            RED_FLAG_CATALOG[f] for f in red_flags
        ))
    if breaches:
        parts.append("；".join(b.message for b in breaches))
    return "；".join(parts)
