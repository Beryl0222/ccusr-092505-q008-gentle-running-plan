"""按角色裁剪的只读视图：最小必要信息。

- 教练只能看到与其有指导关系的学员；健康信息只暴露执教所需的训练限制，
  不暴露血压原值、医生意见原文等隐私字段。
- 学员可查看自己的完整训练安排、暂停原因与恢复条件。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .events import EventType
from .ids import participant_stream, plan_stream, safety_stream
from .plan import PlanService
from .projections import fold_participant
from .safety import SafetyService
from .store import EventStore


class AccessDenied(PermissionError):
    pass


class Roster:
    """教练-学员指导关系表。应用层负责其持久化（JSON）。"""

    def __init__(self, assignments: Mapping[str, set[str]] | None = None) -> None:
        self._by_coach: dict[str, set[str]] = {
            coach: set(members) for coach, members in (assignments or {}).items()
        }

    def assign(self, coach_id: str, participant_id: str) -> None:
        self._by_coach.setdefault(coach_id, set()).add(participant_id)

    def revoke(self, coach_id: str, participant_id: str) -> None:
        self._by_coach.get(coach_id, set()).discard(participant_id)

    def participants_for(self, coach_id: str) -> set[str]:
        return set(self._by_coach.get(coach_id, ()))

    def is_coach_of(self, coach_id: str, participant_id: str) -> bool:
        return participant_id in self._by_coach.get(coach_id, set())

    def save(self, path: str | os.PathLike[str]) -> None:
        Path(path).write_text(
            json.dumps({k: sorted(v) for k, v in self._by_coach.items()},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> Roster:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls({k: set(v) for k, v in data.items()})


# 教练视图允许出现的字段白名单（最小必要）
_COACH_AUTH_FIELDS = (
    "max_heart_rate", "max_rpe", "max_session_minutes",
    "max_jog_bout_seconds", "walk_jog_ratio", "allowed_modes",
    "supervision_required", "supervised_sessions", "red_flags",
)


@dataclass(frozen=True)
class NextSession:
    seq: int
    scheduled_at: str
    total_minutes: int
    note: str
    on_hold: bool


class ViewService:
    def __init__(self, store: EventStore, roster: Roster) -> None:
        self.store = store
        self.roster = roster
        self.plans = PlanService(store)

    # -- 教练视图 -----------------------------------------------------------

    def coach_participant_summary(
        self, coach_id: str, participant_id: str
    ) -> dict[str, Any]:
        if not self.roster.is_coach_of(coach_id, participant_id):
            raise AccessDenied("教练只能查看自己学员的信息")
        state = self._state(participant_id)
        auth = state.authorization
        summary: dict[str, Any] = {
            "participant_id": participant_id,
            "may_train": state.may_train,
            "paused": state.paused,
            # 仅暴露执教所需的安全状态，不提供病史明细
            "requires_supervision": auth.supervision_required if auth else None,
        }
        if auth is not None:
            payload = auth.to_dict()
            summary["training_limits"] = {
                key: payload[key] for key in _COACH_AUTH_FIELDS
            }
        if state.paused:
            pause = SafetyService.current_pause(self.store, participant_id) or {}
            # 教练需要知道是什么类型的风险才能保护学员，但不展开病史
            summary["pause"] = {
                "trigger": pause.get("trigger"),
                "reason": pause.get("reason"),
                "red_flag_codes": [f["code"] for f in pause.get("red_flags", [])],
            }
        nxt = self._next_session(participant_id)
        if nxt is not None:
            summary["next_session"] = {
                "seq": nxt.seq,
                "scheduled_at": nxt.scheduled_at,
                "on_hold": nxt.on_hold,
            }
        return summary

    def coach_dashboard(self, coach_id: str) -> list[dict[str, Any]]:
        return [
            self.coach_participant_summary(coach_id, pid)
            for pid in sorted(self.roster.participants_for(coach_id))
        ]

    # -- 学员视图 -----------------------------------------------------------

    def participant_view(
        self, viewer_id: str, participant_id: str
    ) -> dict[str, Any]:
        if viewer_id != participant_id:
            raise AccessDenied("只能查看本人的处方信息")
        state = self._state(participant_id)
        view: dict[str, Any] = {
            "participant_id": participant_id,
            "may_train": state.may_train,
            "paused": state.paused,
        }
        if state.authorization is not None:
            view["authorization"] = state.authorization.to_dict()
        if state.paused:
            pause = SafetyService.current_pause(self.store, participant_id) or {}
            view["pause"] = {
                "reason": pause.get("reason"),
                "trigger": pause.get("trigger"),
                "occurred_at": str(state.paused_at.isoformat())
                if state.paused_at else None,
                "resume_condition": "需由专业人员完成新的复核后方可恢复训练",
            }
        elif state.last_clearance is not None:
            view["last_clearance"] = {
                "at": state.last_clearance.get("occurred_at"),
                "verdict": state.last_clearance.get("verdict"),
            }
        nxt = self._next_session(participant_id)
        if nxt is not None:
            spec = self.plans.effective_session(participant_id, nxt.seq)
            view["next_session"] = {
                "seq": nxt.seq,
                "scheduled_at": nxt.scheduled_at,
                "total_minutes": nxt.total_minutes,
                "intervals": [item.to_dict() for item in spec.intervals],
                "targets": dict(spec.targets),
                "note": nxt.note,
                "on_hold": nxt.on_hold,
            }
        return view

    # -- 共享投影 -----------------------------------------------------------

    def _state(self, participant_id: str):
        events = (
            self.store.stream(participant_stream(participant_id))
            + self.store.stream(safety_stream(participant_id))
            + self.store.stream(plan_stream(participant_id))
        )
        return fold_participant(participant_id, events)

    def _next_session(self, participant_id: str) -> NextSession | None:
        revisions = self.plans.revisions(participant_id)
        if not revisions:
            return None
        completed: set[int] = set()
        for event in self.store.query(event_type=EventType.SESSION_LOGGED):
            if event.payload.get("participant_id") == participant_id:
                seq = event.payload.get("session_seq")
                if seq is not None:
                    completed.add(int(seq))
        latest = revisions[-1]
        upcoming = [
            spec for spec in latest.sessions
            if spec.seq not in completed
        ]
        if not upcoming:
            return None
        spec = upcoming[0]
        paused = SafetyService.is_paused(self.store, participant_id)
        return NextSession(
            seq=spec.seq,
            scheduled_at=spec.scheduled_at,
            total_minutes=spec.total_minutes,
            note=spec.note,
            on_hold=paused,
        )
