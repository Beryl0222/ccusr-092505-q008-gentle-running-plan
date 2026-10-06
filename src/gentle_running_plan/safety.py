"""安全暂停与专业复核恢复。

触发条件：异常报告中的红旗症状，或体征超出授权负荷。暂停立即对后续所有
课次生效；只有暂停之后产生的新专业复核（REVIEW_CLEARED）才能恢复。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from .clock import Clock, SystemClock
from .events import AggregateType, Event, EventType, make_event
from .ids import plan_stream, safety_stream
from .store import EventStore


class SafetyError(ValueError):
    pass


# 红旗症状代码 -> 中文说明。出现任一即暂停。
RED_FLAG_CATALOG: dict[str, str] = {
    "chest_pain": "胸痛、胸闷或放射至手臂/下颌的不适",
    "palpitations": "明显心悸",
    "unexplained_dyspnea": "与运动强度不符的呼吸困难",
    "dizziness": "眩晕、黑蒙或冷汗",
    "throbbing_headache": "搏动性头痛或视物模糊",
    "knee_locking": "膝关节锐痛、卡顿或打软腿",
    "knee_swelling": "训练后关节肿胀或次日疼痛加重",
}


@dataclass(frozen=True)
class ThresholdBreach:
    metric: str
    observed: float | int
    limit: float | int
    rule: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "observed": self.observed,
            "limit": self.limit,
            "rule": self.rule,
            "message": self.message,
        }


def evaluate_breaches(
    readings: Mapping[str, Any],
    authorization_snapshot: Mapping[str, Any],
) -> list[ThresholdBreach]:
    """把一次上报的体征与当时适用的授权门槛对照。"""
    breaches: list[ThresholdBreach] = []
    max_hr = authorization_snapshot.get("max_heart_rate")
    peak_hr = readings.get("peak_heart_rate")
    if max_hr is not None and isinstance(peak_hr, (int, float)) and peak_hr > max_hr:
        breaches.append(ThresholdBreach(
            metric="peak_heart_rate",
            observed=peak_hr,
            limit=max_hr,
            rule="AUTH-HR",
            message=f"峰值心率 {peak_hr} 超过授权上限 {max_hr}",
        ))
    max_rpe = authorization_snapshot.get("max_rpe")
    rpe = readings.get("rpe")
    if max_rpe is not None and isinstance(rpe, (int, float)) and rpe > max_rpe:
        breaches.append(ThresholdBreach(
            metric="rpe",
            observed=rpe,
            limit=max_rpe,
            rule="AUTH-RPE",
            message=f"主观强度 RPE {rpe} 超过授权上限 {max_rpe}",
        ))
    max_minutes = authorization_snapshot.get("max_session_minutes")
    duration = readings.get("duration_minutes")
    if (max_minutes is not None and isinstance(duration, (int, float))
            and duration > max_minutes):
        breaches.append(ThresholdBreach(
            metric="duration_minutes",
            observed=duration,
            limit=max_minutes,
            rule="AUTH-DURATION",
            message=f"训练时长 {duration} 分钟超过授权上限 {max_minutes} 分钟",
        ))
    return breaches


class SafetyService:
    def __init__(self, store: EventStore, clock: Clock | None = None) -> None:
        self.store = store
        self.clock = clock or SystemClock()

    @staticmethod
    def is_paused(store: EventStore, participant_id: str) -> bool:
        last = SafetyService._last_pause_event(store, participant_id)
        if last is None:
            return False
        # 只有显式恢复本次暂停的事件才能解除；针对更早暂停的恢复
        # 不会抵消新的暂停。
        return not any(
            event.event_type == EventType.PLAN_RESUMED
            and event.payload.get("pause_event_id") == last.event_id
            for event in store.stream(plan_stream(participant_id))
        )

    @staticmethod
    def _last_pause_event(store: EventStore, participant_id: str) -> Event | None:
        pauses = [
            e for e in store.stream(safety_stream(participant_id))
            if e.event_type == EventType.SAFETY_PAUSED
        ]
        return pauses[-1] if pauses else None

    @staticmethod
    def current_pause(store: EventStore, participant_id: str) -> dict[str, Any] | None:
        if not SafetyService.is_paused(store, participant_id):
            return None
        last = SafetyService._last_pause_event(store, participant_id)
        return dict(last.payload) if last else None

    def pause(
        self,
        participant_id: str,
        *,
        reason: str,
        trigger: str,
        source_event_id: str | None = None,
        red_flags: Sequence[str] | None = None,
        breaches: Sequence[ThresholdBreach | Mapping[str, Any]] | None = None,
        detail: Mapping[str, Any] | None = None,
        occurred_at: datetime | str | None = None,
    ) -> Event:
        """立即暂停后续训练。重复暂停会叠加原因，但仍只有一次恢复动作。"""
        if not reason.strip():
            raise SafetyError("暂停必须写明原因")
        stream_id = safety_stream(participant_id)
        version = self.store.version_of(stream_id) + 1
        breach_payload = [
            b.to_dict() if isinstance(b, ThresholdBreach) else dict(b)
            for b in (breaches or [])
        ]
        flags = list(dict.fromkeys(red_flags or []))
        unknown = [f for f in flags if f not in RED_FLAG_CATALOG]
        if unknown:
            raise SafetyError(f"未登记的红旗症状代码: {unknown}")
        event = make_event(
            EventType.SAFETY_PAUSED,
            AggregateType.SAFETY_REPORT,
            stream_id,
            occurred_at or self.clock.now(),
            version=version,
            summary=f"安全暂停：{reason}",
            payload={
                "participant_id": participant_id,
                "reason": reason,
                "trigger": trigger,  # red_flag / threshold_breach / manual
                "source_event_id": source_event_id,
                "red_flags": [
                    {"code": f, "label": RED_FLAG_CATALOG[f]} for f in flags
                ],
                "breaches": breach_payload,
                "detail": dict(detail or {}),
            },
        )
        return self.store.append(event)

    def resume_after_review(
        self,
        participant_id: str,
        *,
        review_event_id: str,
        occurred_at: datetime | str | None = None,
    ) -> Event:
        """凭暂停之后的新专业复核恢复训练。"""
        if not self.is_paused(self.store, participant_id):
            raise SafetyError("当前没有处于暂停状态")
        review = self.store.find(review_event_id)
        if review is None:
            raise SafetyError("复核事件不存在")
        if review.event_type != EventType.REVIEW_CLEARED:
            raise SafetyError("只有结论为通过的新专业复核才能恢复训练")
        if review.payload.get("participant_id") != participant_id:
            raise SafetyError("复核与参与者不匹配")
        last_pause = self._last_pause_event(self.store, participant_id)
        assert last_pause is not None
        # "新的专业复核"以显式引用本次暂停事件为准：暂停前的旧复核
        # 不可能携带当前 pause_event_id。
        if review.payload.get("pause_event_id") != last_pause.event_id:
            raise SafetyError(
                "不能用针对其他暂停（或暂停之前）的旧复核恢复；"
                "必须完成新的专业复核"
            )
        if not review.payload.get("clears_pause"):
            raise SafetyError("该复核未声明解除暂停，请重新复核并确认")
        stream_id = plan_stream(participant_id)
        version = self.store.version_of(stream_id) + 1
        event = make_event(
            EventType.PLAN_RESUMED,
            AggregateType.EXERCISE_PLAN,
            stream_id,
            occurred_at or self.clock.now(),
            version=version,
            summary="专业复核通过，恢复训练（后续课次生效）",
            payload={
                "participant_id": participant_id,
                "review_event_id": review_event_id,
                "pause_event_id": last_pause.event_id,
            },
        )
        return self.store.append(event)
