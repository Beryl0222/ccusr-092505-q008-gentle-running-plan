"""筛查登记与专业复核服务。

筛查本身是不可改写的事实：即使规则判定暂不准入，也登记 SCREENING_SUBMITTED
并附完整授权推导；通过时登记 SCREENING_APPROVED。任何后续复核都是新事件，
不覆盖原始筛查。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .clock import Clock, SystemClock
from .events import AggregateType, Event, EventType, make_event
from .ids import participant_stream
from .rules import (
    HealthProfile,
    derive_authorization,
)
from .store import EventStore


class ScreeningError(ValueError):
    pass


class ScreeningService:
    def __init__(self, store: EventStore, clock: Clock | None = None) -> None:
        self.store = store
        self.clock = clock or SystemClock()

    def submit(
        self, profile: HealthProfile, *, occurred_at: datetime | str | None = None
    ) -> Event:
        """登记一次健康筛查并推导授权负荷。每人只能建立一次筛查流。"""
        stream_id = participant_stream(profile.participant_id)
        if self.store.stream(stream_id):
            raise ScreeningError(
                f"参与者 {profile.participant_id} 已有筛查记录；"
                "新情况请提交专业复核，不得改写原始筛查"
            )
        occurred_at = occurred_at or self.clock.now()
        auth = derive_authorization(profile)
        event_type = (
            EventType.SCREENING_APPROVED
            if auth.authorized
            else EventType.SCREENING_SUBMITTED
        )
        summary = (
            "筛查通过，已生成授权负荷"
            if auth.authorized
            else "筛查登记完成，需专业复核后才能开训"
        )
        event = make_event(
            event_type,
            AggregateType.PARTICIPANT_SCREENING,
            stream_id,
            occurred_at,
            version=1,
            summary=summary,
            payload={
                "participant_id": profile.participant_id,
                "profile": _profile_to_payload(profile),
                "authorization": auth.to_dict(),
            },
        )
        return self.store.append(event)

    def record_review(
        self,
        participant_id: str,
        *,
        reviewer_id: str,
        verdict: str,
        note: str = "",
        profile_updates: Mapping[str, Any] | None = None,
        clear_pause: bool = False,
        occurred_at: datetime | str | None = None,
    ) -> Event:
        """登记一次专业复核。

        复核基于最新筛查画像（可携带血压/膝伤结案等更新）重新推导授权；
        clear_pause=True 表示该复核同时用于解除安全暂停，调用方随后可凭本
        事件让 SafetyService 恢复训练。
        """
        stream_id = participant_stream(participant_id)
        history = self.store.stream(stream_id)
        if not history:
            raise ScreeningError("尚未登记筛查，无法复核")
        latest = next(
            e for e in reversed(history)
            if e.event_type in {
                EventType.SCREENING_APPROVED,
                EventType.SCREENING_SUBMITTED,
            }
        )
        profile = HealthProfile.from_dict(latest.payload["profile"])
        if profile_updates:
            profile = _update_profile(profile, profile_updates)
        if verdict not in {"approved", "restricted", "rejected"}:
            raise ScreeningError("verdict 必须是 approved/restricted/rejected")
        # 复核结论覆盖医生意见字段
        payload = _profile_to_payload(profile)
        payload["doctor_verdict"] = verdict
        payload["doctor_note"] = note or profile.doctor_note
        profile = HealthProfile.from_dict(payload)
        auth = derive_authorization(profile)
        occurred_at = occurred_at or self.clock.now()
        cleared = auth.authorized and verdict in {"approved", "restricted"}
        # 若该复核用于解除暂停，显式记录它针对的是哪一次暂停；
        # 恢复时据此确认"新复核"，避免用暂停前的旧结论恢复。
        pause_event_id: str | None = None
        if clear_pause and cleared:
            from .safety import SafetyService

            last_pause = SafetyService._last_pause_event(self.store, participant_id)
            if last_pause is not None and SafetyService.is_paused(
                self.store, participant_id
            ):
                pause_event_id = last_pause.event_id
        event_type = EventType.REVIEW_CLEARED if cleared else EventType.REVIEW_RECORDED
        summary = (
            f"专业复核 {verdict}，{'恢复准入' if cleared else '仍需暂缓'}"
        )
        event = make_event(
            event_type,
            AggregateType.PARTICIPANT_SCREENING,
            stream_id,
            occurred_at,
            version=len(history) + 1,
            summary=summary,
            payload={
                "participant_id": participant_id,
                "reviewer_id": reviewer_id,
                "verdict": verdict,
                "note": note,
                "clears_pause": bool(clear_pause and cleared and pause_event_id),
                "pause_event_id": pause_event_id,
                "profile": _profile_to_payload(profile),
                "authorization": auth.to_dict(),
            },
        )
        return self.store.append(event, expected_version=len(history))


def _profile_to_payload(profile: HealthProfile) -> dict[str, Any]:
    return {
        "participant_id": profile.participant_id,
        "age": profile.age,
        "knee": profile.knee.value,
        "resting_sbp": profile.resting_sbp,
        "resting_dbp": profile.resting_dbp,
        "hypertension_controlled": profile.hypertension_controlled,
        "rehab": profile.rehab.value,
        "rehab_weeks_since": profile.rehab_weeks_since,
        "doctor_verdict": profile.doctor_verdict.value,
        "doctor_overrides": dict(profile.doctor_overrides),
        "doctor_note": profile.doctor_note,
        "footwear": profile.footwear.value,
        "surface": profile.surface.value,
        "beginner": profile.beginner,
        "walk_capacity_minutes": profile.walk_capacity_minutes,
        "goals": list(profile.goals),
    }


def _update_profile(
    profile: HealthProfile, updates: Mapping[str, Any]
) -> HealthProfile:
    data = _profile_to_payload(profile)
    data.update(dict(updates))
    return HealthProfile.from_dict(data)
