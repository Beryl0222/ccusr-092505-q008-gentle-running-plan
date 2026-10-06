"""领域事件信封与已登记类型。

所有业务事实都以事件表达；服务只追加事件，不改写历史。信封字段与
``contracts/domain.schema.json`` 保持一致，新增事件类型须同步登记。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping
from uuid import uuid4

from .clock import parse_time


class EventType(StrEnum):
    SCREENING_APPROVED = "SCREENING_APPROVED"
    SCREENING_SUBMITTED = "SCREENING_SUBMITTED"
    REVIEW_RECORDED = "REVIEW_RECORDED"
    REVIEW_CLEARED = "REVIEW_CLEARED"
    PLAN_PUBLISHED = "PLAN_PUBLISHED"
    PLAN_ADJUSTED = "PLAN_ADJUSTED"
    SESSION_LOGGED = "SESSION_LOGGED"
    REPORT_CONFLICTED = "REPORT_CONFLICTED"
    SAFETY_PAUSED = "SAFETY_PAUSED"
    PLAN_RESUMED = "PLAN_RESUMED"
    REMINDER_REQUESTED = "REMINDER_REQUESTED"
    REMINDER_DELIVERED = "REMINDER_DELIVERED"
    REMINDER_CANCELLED = "REMINDER_CANCELLED"


class AggregateType(StrEnum):
    PARTICIPANT_SCREENING = "participant_screening"
    EXERCISE_PLAN = "exercise_plan"
    TRAINING_SESSION = "training_session"
    SAFETY_REPORT = "safety_report"
    REMINDER = "reminder"


# 已登记事件允许出现的聚合类型
EVENT_AGGREGATES: dict[str, frozenset[AggregateType]] = {
    EventType.SCREENING_APPROVED: frozenset({AggregateType.PARTICIPANT_SCREENING}),
    EventType.SCREENING_SUBMITTED: frozenset({AggregateType.PARTICIPANT_SCREENING}),
    EventType.REVIEW_RECORDED: frozenset({AggregateType.PARTICIPANT_SCREENING}),
    EventType.REVIEW_CLEARED: frozenset({AggregateType.PARTICIPANT_SCREENING}),
    EventType.PLAN_PUBLISHED: frozenset({AggregateType.EXERCISE_PLAN}),
    EventType.PLAN_ADJUSTED: frozenset({AggregateType.EXERCISE_PLAN}),
    EventType.SESSION_LOGGED: frozenset({AggregateType.TRAINING_SESSION}),
    EventType.REPORT_CONFLICTED: frozenset({AggregateType.TRAINING_SESSION}),
    EventType.SAFETY_PAUSED: frozenset({AggregateType.SAFETY_REPORT}),
    EventType.PLAN_RESUMED: frozenset({AggregateType.EXERCISE_PLAN}),
    EventType.REMINDER_REQUESTED: frozenset({AggregateType.REMINDER}),
    EventType.REMINDER_DELIVERED: frozenset({AggregateType.REMINDER}),
    EventType.REMINDER_CANCELLED: frozenset({AggregateType.REMINDER}),
}


class EventError(ValueError):
    """信封或存储层违反契约。"""


@dataclass(frozen=True)
class Event:
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: datetime
    version: int
    summary: str
    payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        data = {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "occurred_at": self.occurred_at.isoformat(),
            "version": self.version,
            "summary": self.summary,
        }
        data.update(self.payload)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Event:
        reserved = {
            "event_id", "event_type", "aggregate_type", "aggregate_id",
            "occurred_at", "version", "summary",
        }
        return cls(
            event_id=str(data["event_id"]),
            event_type=str(data["event_type"]),
            aggregate_type=str(data["aggregate_type"]),
            aggregate_id=str(data["aggregate_id"]),
            occurred_at=parse_time(str(data["occurred_at"])),
            version=int(data["version"]),
            summary=str(data["summary"]),
            payload={k: v for k, v in data.items() if k not in reserved},
        )


def make_event(
    event_type: EventType | str,
    aggregate_type: AggregateType | str,
    aggregate_id: str,
    occurred_at: datetime | str,
    version: int,
    summary: str,
    *,
    event_id: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> Event:
    """构造并校验信封。不在交换层补默认业务值。"""
    event_type = EventType(event_type)
    aggregate_type = AggregateType(aggregate_type)
    if aggregate_type not in EVENT_AGGREGATES[event_type]:
        raise EventError(f"事件 {event_type} 不允许聚合类型 {aggregate_type}")
    if not aggregate_id.strip():
        raise EventError("aggregate_id 不能为空")
    if isinstance(occurred_at, str):
        occurred_at = parse_time(occurred_at)
    if occurred_at.tzinfo is None:
        raise EventError("occurred_at 必须包含时区")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise EventError("version 必须是正整数")
    if not summary.strip():
        raise EventError("summary 不能为空")
    return Event(
        event_id=event_id or f"evt-{uuid4().hex[:12]}",
        event_type=event_type.value,
        aggregate_type=aggregate_type.value,
        aggregate_id=aggregate_id,
        occurred_at=occurred_at,
        version=version,
        summary=summary,
        payload=dict(payload or {}),
    )
