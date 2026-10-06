"""版本化训练处方。

每次发布或调整都是计划流上的一个新版本，不可覆盖旧版本；调整必须声明
``effective_from_seq``——从未来哪一节课开始生效，已完成课次一律不受影响。
发布时把授权负荷快照（含规则编号）固化进计划，执行时按课次选择适用版本。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from .clock import Clock, SystemClock
from .events import AggregateType, Event, EventType, make_event
from .ids import participant_stream, plan_stream
from .projections import _authorization_from_dict
from .rules import RiskAuthorization
from .store import EventStore


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class Interval:
    mode: str           # walk / slow_jog
    seconds: int
    repeat: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "seconds": self.seconds, "repeat": self.repeat}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Interval:
        return cls(
            mode=str(data["mode"]),
            seconds=int(data["seconds"]),
            repeat=int(data.get("repeat", 1)),
        )


@dataclass(frozen=True)
class SessionSpec:
    seq: int
    scheduled_at: str          # 带时区 ISO 时间
    warmup_minutes: int
    intervals: tuple[Interval, ...]
    cooldown_minutes: int
    targets: Mapping[str, Any]
    note: str = ""

    @property
    def work_seconds(self) -> int:
        return sum(item.seconds * item.repeat for item in self.intervals)

    @property
    def total_minutes(self) -> int:
        return self.warmup_minutes + self.cooldown_minutes + self.work_seconds // 60

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "scheduled_at": self.scheduled_at,
            "warmup_minutes": self.warmup_minutes,
            "intervals": [item.to_dict() for item in self.intervals],
            "cooldown_minutes": self.cooldown_minutes,
            "total_minutes": self.total_minutes,
            "targets": dict(self.targets),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SessionSpec:
        return cls(
            seq=int(data["seq"]),
            scheduled_at=str(data["scheduled_at"]),
            warmup_minutes=int(data["warmup_minutes"]),
            intervals=tuple(
                Interval.from_dict(item) for item in data.get("intervals", ())
            ),
            cooldown_minutes=int(data["cooldown_minutes"]),
            targets=dict(data.get("targets", {})),
            note=str(data.get("note", "")),
        )


@dataclass(frozen=True)
class PlanViolation:
    seq: int | None
    code: str
    message: str


@dataclass(frozen=True)
class PlanRevision:
    plan_version: int
    effective_from_seq: int
    reason: str
    issued_at: datetime
    authorization_snapshot: Mapping[str, Any]
    sessions: tuple[SessionSpec, ...]

    def session(self, seq: int) -> SessionSpec | None:
        for spec in self.sessions:
            if spec.seq == seq:
                return spec
        return None


def validate_sessions(
    sessions: Sequence[SessionSpec], auth: RiskAuthorization
) -> list[PlanViolation]:
    """逐课对照授权负荷，返回所有违例（不抛异常，便于批量汇报）。"""
    violations: list[PlanViolation] = []
    for spec in sessions:
        if not auth.authorized:
            violations.append(PlanViolation(
                spec.seq, "not_authorized", "当前授权状态不允许安排训练"
            ))
            continue
        if spec.total_minutes > (auth.max_session_minutes or 0):
            violations.append(PlanViolation(
                spec.seq, "session_too_long",
                f"第 {spec.seq} 课 {spec.total_minutes} 分钟超过授权上限 "
                f"{auth.max_session_minutes} 分钟",
            ))
        jog_bouts = [i for i in spec.intervals if i.mode == "slow_jog"]
        if jog_bouts and "slow_jog" not in auth.allowed_modes:
            violations.append(PlanViolation(
                spec.seq, "mode_forbidden",
                f"第 {spec.seq} 课包含未授权动作 slow_jog",
            ))
        else:
            for bout in jog_bouts:
                if (auth.max_jog_bout_seconds is not None
                      and bout.seconds > auth.max_jog_bout_seconds):
                    violations.append(PlanViolation(
                        spec.seq, "jog_bout_too_long",
                        f"第 {spec.seq} 课慢跑段 {bout.seconds} 秒超过授权上限 "
                        f"{auth.max_jog_bout_seconds} 秒",
                    ))
        if auth.walk_jog_ratio is not None and jog_bouts:
            walk_seconds = sum(
                i.seconds * i.repeat for i in spec.intervals if i.mode == "walk"
            )
            jog_seconds = sum(
                i.seconds * i.repeat for i in jog_bouts
            )
            w, j = auth.walk_jog_ratio
            if jog_seconds and walk_seconds * j < w * jog_seconds:
                violations.append(PlanViolation(
                    spec.seq, "walk_jog_ratio",
                    f"第 {spec.seq} 课走:跑为 {walk_seconds}:{jog_seconds}，"
                    f"低于授权要求 {w}:{j}",
                ))
        cap_hr = spec.targets.get("max_heart_rate")
        if cap_hr is not None and cap_hr > (auth.max_heart_rate or 0):
            violations.append(PlanViolation(
                spec.seq, "heart_rate_target",
                f"第 {spec.seq} 课心率目标 {cap_hr} 超过授权上限 "
                f"{auth.max_heart_rate}",
            ))
        cap_rpe = spec.targets.get("max_rpe")
        if cap_rpe is not None and cap_rpe > (auth.max_rpe or 0):
            violations.append(PlanViolation(
                spec.seq, "rpe_target",
                f"第 {spec.seq} 课 RPE 目标 {cap_rpe} 超过授权上限 "
                f"{auth.max_rpe}",
            ))
    return violations


class PlanService:
    def __init__(self, store: EventStore, clock: Clock | None = None) -> None:
        self.store = store
        self.clock = clock or SystemClock()

    # -- 内部投影 -----------------------------------------------------------

    def _latest_authorization(self, participant_id: str) -> RiskAuthorization:
        history = self.store.stream(participant_stream(participant_id))
        if not history:
            raise PlanError("参与者尚未完成筛查")
        for event in reversed(history):
            if event.event_type in {
                EventType.SCREENING_APPROVED,
                EventType.SCREENING_SUBMITTED,
                EventType.REVIEW_RECORDED,
                EventType.REVIEW_CLEARED,
            }:
                return _authorization_from_dict(
                    participant_id, event.payload["authorization"]
                )
        raise PlanError("参与者尚未完成筛查")

    def _is_paused(self, participant_id: str) -> bool:
        from .safety import SafetyService

        return SafetyService.is_paused(self.store, participant_id)

    def completed_seqs(self, participant_id: str) -> set[int]:
        seqs: set[int] = set()
        for event in self.store.query(event_type=EventType.SESSION_LOGGED):
            if event.payload.get("participant_id") == participant_id:
                seqs.add(int(event.payload["session_seq"]))
        return seqs

    def revisions(self, participant_id: str) -> list[PlanRevision]:
        events = self.store.stream(plan_stream(participant_id))
        revisions: list[PlanRevision] = []
        for event in events:
            if event.event_type not in {
                EventType.PLAN_PUBLISHED, EventType.PLAN_ADJUSTED
            }:
                continue
            revisions.append(
                PlanRevision(
                    plan_version=int(event.payload["plan_version"]),
                    effective_from_seq=int(event.payload["effective_from_seq"]),
                    reason=str(event.payload.get("reason", "")),
                    issued_at=event.occurred_at,
                    authorization_snapshot=event.payload.get(
                        "authorization_snapshot", {}
                    ),
                    sessions=tuple(
                        SessionSpec.from_dict(item)
                        for item in event.payload.get("sessions", ())
                    ),
                )
            )
        return revisions

    def effective_session(self, participant_id: str, seq: int) -> SessionSpec:
        """某一课次当前适用的版本：取生效点 <= seq 的最新修订。"""
        candidates = [
            rev for rev in self.revisions(participant_id)
            if rev.effective_from_seq <= seq and rev.session(seq) is not None
        ]
        if not candidates:
            raise PlanError(f"第 {seq} 课没有适用的计划版本")
        return candidates[-1].session(seq)  # type: ignore[return-value]

    def effective_authorization(self, participant_id: str, seq: int) -> Mapping[str, Any]:
        candidates = [
            rev for rev in self.revisions(participant_id)
            if rev.effective_from_seq <= seq and rev.session(seq) is not None
        ]
        if not candidates:
            raise PlanError(f"第 {seq} 课没有适用的计划版本")
        return candidates[-1].authorization_snapshot

    # -- 写入 ---------------------------------------------------------------

    def publish(
        self,
        participant_id: str,
        sessions: Sequence[SessionSpec],
        *,
        reason: str = "初版处方",
        occurred_at: datetime | str | None = None,
    ) -> Event:
        if self.store.stream(plan_stream(participant_id)):
            raise PlanError("计划已存在，调整请使用 adjust")
        if self._is_paused(participant_id):
            raise PlanError("训练处于安全暂停状态，须先经专业复核恢复")
        auth = self._latest_authorization(participant_id)
        if not auth.authorized:
            raise PlanError("筛查未通过或仍需专业复核，不能发布计划")
        violations = validate_sessions(sessions, auth)
        if violations:
            raise PlanError(
                "计划超出授权负荷: "
                + "；".join(v.message for v in violations)
            )
        self._check_sequence(sessions)
        event = make_event(
            EventType.PLAN_PUBLISHED,
            AggregateType.EXERCISE_PLAN,
            plan_stream(participant_id),
            occurred_at or self.clock.now(),
            version=1,
            summary=f"发布初版处方：{reason}",
            payload={
                "participant_id": participant_id,
                "plan_version": 1,
                "effective_from_seq": 1,
                "reason": reason,
                "authorization_snapshot": _snapshot(auth),
                "sessions": [spec.to_dict() for spec in sessions],
            },
        )
        return self.store.append(event)

    def adjust(
        self,
        participant_id: str,
        sessions: Sequence[SessionSpec],
        *,
        effective_from_seq: int,
        reason: str,
        occurred_at: datetime | str | None = None,
    ) -> Event:
        """发布调整版本。

        - effective_from_seq 必须是尚未完成的未来课次；
        - 早于生效点的课次必须与上一版逐字一致（历史不可变）；
        - 新课表整体仍须满足当前授权负荷。
        """
        if not reason.strip():
            raise PlanError("调整必须写明原因")
        revisions = self.revisions(participant_id)
        if not revisions:
            raise PlanError("尚未发布初版计划")
        if self._is_paused(participant_id):
            raise PlanError("训练处于安全暂停状态，须先经专业复核恢复")
        completed = self.completed_seqs(participant_id)
        if completed and effective_from_seq <= max(completed):
            raise PlanError(
                f"生效课次 {effective_from_seq} 已完成；调整只能对未来课次生效，"
                "已完成训练不会被改写"
            )
        previous = revisions[-1]
        new_version = previous.plan_version + 1
        self._check_sequence(sessions)

        old_by_seq = {s.seq: s for s in previous.sessions}
        new_by_seq = {s.seq: s for s in sessions}
        for seq in sorted(old_by_seq):
            if seq < effective_from_seq and old_by_seq[seq].to_dict() != (
                new_by_seq.get(seq).to_dict() if new_by_seq.get(seq) else None
            ):
                raise PlanError(
                    f"第 {seq} 课早于生效点 {effective_from_seq}，必须保持不变"
                )

        auth = self._latest_authorization(participant_id)
        future = [s for s in sessions if s.seq >= effective_from_seq]
        violations = validate_sessions(future, auth)
        if violations:
            raise PlanError(
                "调整后的课次超出授权负荷: "
                + "；".join(v.message for v in violations)
            )

        stream_id = plan_stream(participant_id)
        current_version = self.store.version_of(stream_id)
        event = make_event(
            EventType.PLAN_ADJUSTED,
            AggregateType.EXERCISE_PLAN,
            stream_id,
            occurred_at or self.clock.now(),
            version=current_version + 1,
            summary=f"自第 {effective_from_seq} 课起调整处方：{reason}",
            payload={
                "participant_id": participant_id,
                "plan_version": new_version,
                "effective_from_seq": effective_from_seq,
                "reason": reason,
                "supersedes_version": previous.plan_version,
                "authorization_snapshot": _snapshot(auth),
                "sessions": [spec.to_dict() for spec in sessions],
            },
        )
        return self.store.append(event, expected_version=current_version)

    @staticmethod
    def _check_sequence(sessions: Sequence[SessionSpec]) -> None:
        seqs = sorted(spec.seq for spec in sessions)
        if not seqs or seqs[0] != 1 or seqs != list(range(1, len(seqs) + 1)):
            raise PlanError("课次编号必须从 1 开始且连续")
        for spec in sessions:
            if any(i.seconds <= 0 or i.repeat <= 0 for i in spec.intervals):
                raise PlanError(f"第 {spec.seq} 课间歇时长与次数必须为正")


def _snapshot(auth: RiskAuthorization) -> dict[str, Any]:
    return {
        "max_heart_rate": auth.max_heart_rate,
        "max_rpe": auth.max_rpe,
        "max_session_minutes": auth.max_session_minutes,
        "max_jog_bout_seconds": auth.max_jog_bout_seconds,
        "walk_jog_ratio": list(auth.walk_jog_ratio)
        if auth.walk_jog_ratio else None,
        "allowed_modes": list(auth.allowed_modes),
        "rule_ids": [decision.rule_id for decision in auth.basis],
    }
