"""参与者聚合投影：把事件流折叠成当前筛查/授权/暂停状态。

投影只读事件，不修改历史；任何"当前状态"都能从事件重新算出。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .events import Event, EventType
from .rules import HealthProfile, RiskAuthorization


@dataclass
class ParticipantState:
    participant_id: str
    screening: HealthProfile | None = None
    authorization: RiskAuthorization | None = None
    screening_version: int = 0
    paused: bool = False
    pause_reason: str = ""
    pause_causes: tuple[dict[str, Any], ...] = ()
    pause_event_id: str | None = None
    paused_at: Any = None
    last_clearance: dict[str, Any] | None = None

    @property
    def may_train(self) -> bool:
        return (
            self.authorization is not None
            and self.authorization.authorized
            and not self.paused
        )

    @property
    def requires_review(self) -> bool:
        return self.paused or (
            self.authorization is not None
            and self.authorization.requires_professional_review
        )


def fold_participant(participant_id: str, events: list[Event]) -> ParticipantState:
    """按发生顺序折叠参与者流、安全流与计划流事件。"""
    state = ParticipantState(participant_id=participant_id)
    pause_causes: list[dict[str, Any]] = []
    active_pause_id: str | None = None
    for event in sorted(events, key=lambda e: (e.occurred_at, e.version)):
        data = event.payload
        if event.event_type in {
            EventType.SCREENING_APPROVED,
            EventType.SCREENING_SUBMITTED,
        }:
            state.screening = HealthProfile.from_dict(data["profile"])
            state.authorization = _authorization_from_dict(
                participant_id, data["authorization"]
            )
            state.screening_version = event.version
        elif event.event_type in {
            EventType.REVIEW_RECORDED,
            EventType.REVIEW_CLEARED,
        }:
            state.authorization = _authorization_from_dict(
                participant_id, data["authorization"]
            )
            state.screening = HealthProfile.from_dict(data["profile"])
            if event.event_type == EventType.REVIEW_CLEARED:
                state.last_clearance = {
                    **dict(data),
                    "occurred_at": event.occurred_at.isoformat(),
                }
        elif event.event_type == EventType.SAFETY_PAUSED:
            state.paused = True
            state.pause_reason = str(data.get("reason", ""))
            state.pause_event_id = event.event_id
            active_pause_id = event.event_id
            state.paused_at = event.occurred_at
            pause_causes.append(dict(data))
        elif (event.event_type == EventType.PLAN_RESUMED
              and data.get("pause_event_id") == active_pause_id):
            # 仅当恢复事件针对当前暂停时才解除
            state.paused = False
            state.pause_reason = ""
            state.pause_event_id = None
            state.paused_at = None
            active_pause_id = None
            pause_causes = []
    state.pause_causes = tuple(pause_causes)
    return state


def _authorization_from_dict(
    participant_id: str, data: Mapping[str, Any]
) -> RiskAuthorization:
    from .rules import RuleDecision

    return RiskAuthorization(
        participant_id=participant_id,
        authorized=bool(data["authorized"]),
        requires_professional_review=bool(
            data["requires_professional_review"]
        ),
        review_reasons=tuple(data.get("review_reasons", ())),
        max_heart_rate=data.get("max_heart_rate"),
        max_rpe=data.get("max_rpe"),
        max_session_minutes=data.get("max_session_minutes"),
        max_jog_bout_seconds=data.get("max_jog_bout_seconds"),
        walk_jog_ratio=tuple(data["walk_jog_ratio"])
        if data.get("walk_jog_ratio") else None,
        allowed_modes=tuple(data.get("allowed_modes", ())),
        supervision_required=bool(data["supervision_required"]),
        supervised_sessions=int(data.get("supervised_sessions", 0)),
        red_flags=tuple(data.get("red_flags", ())),
        basis=tuple(
            RuleDecision(
                rule_id=d["rule_id"],
                title=d["title"],
                effect=d["effect"],
                severity=d["severity"],
            )
            for d in data.get("basis", ())
        ),
        doctor_note=data.get("doctor_note", ""),
    )


def authorization_to_payload(auth: RiskAuthorization) -> dict[str, Any]:
    return auth.to_dict()
