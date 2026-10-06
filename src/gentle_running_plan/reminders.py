"""课次提醒队列：重启续处理、每条约一次、暂停期不打扰。

提醒状态完全建立在事件存储上（REMINDER_REQUESTED / DELIVERED / CANCELLED），
服务重启后从事件重建待发队列；投递沿注入的日历判断到期。每条提醒使用
确定性流标识，计划调整后旧版本待发提醒作废、新版本重新排期。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from .clock import Clock, SystemClock, parse_time
from .events import AggregateType, Event, EventType, make_event
from .ids import reminder_stream, session_reminder_id
from .plan import PlanService
from .safety import SafetyService
from .store import EventStore


class NotificationSender(Protocol):
    def send(self, message: dict[str, Any]) -> None:
        """实际下发通道（短信/App/教练表格）。失败时抛异常，提醒不计已发。"""


@dataclass(frozen=True)
class Reminder:
    reminder_id: str
    participant_id: str
    plan_version: int
    session_seq: int
    remind_at: datetime
    scheduled_at: str
    channel: str
    stream_id: str

    def message(self) -> dict[str, Any]:
        return {
            "reminder_id": self.reminder_id,
            "participant_id": self.participant_id,
            "session_seq": self.session_seq,
            "plan_version": self.plan_version,
            "scheduled_at": self.scheduled_at,
            "channel": self.channel,
            "text": f"提醒：第 {self.session_seq} 节课安排在 {self.scheduled_at}",
        }


class ReminderService:
    def __init__(
        self,
        store: EventStore,
        clock: Clock | None = None,
        sender: NotificationSender | None = None,
        default_lead_minutes: int = 60,
    ) -> None:
        self.store = store
        self.clock = clock or SystemClock()
        self.sender = sender
        self.lead_minutes = default_lead_minutes
        self.plans = PlanService(store)

    # -- 排期 ---------------------------------------------------------------

    def schedule_for_plan(
        self,
        participant_id: str,
        *,
        channel: str = "app",
        lead_minutes: int | None = None,
        now: datetime | str | None = None,
    ) -> list[Event]:
        """为当前最新版本的未来课次排提醒；已存在的流不重复创建。"""
        moment = parse_time(now) if isinstance(now, str) else (now or self.clock.now())
        lead = self.lead_minutes if lead_minutes is None else lead_minutes
        revisions = self.plans.revisions(participant_id)
        if not revisions:
            return []
        revision = revisions[-1]
        created: list[Event] = []
        completed_seqs = self.plans.completed_seqs(participant_id)
        from datetime import timedelta

        for spec in revision.sessions:
            if spec.seq in completed_seqs:
                continue
            reminder_id = session_reminder_id(
                participant_id, revision.plan_version, spec.seq
            )
            stream_id = reminder_stream(reminder_id)
            if self.store.stream(stream_id):
                continue  # 幂等：重启/重复排期不产生第二条
            if self._has_active_reminder(participant_id, spec.seq):
                continue  # 该课次已有其他版本的有效提醒，不重复打扰
            scheduled_at = parse_time(spec.scheduled_at)
            remind_at = scheduled_at - timedelta(minutes=lead)
            event = make_event(
                EventType.REMINDER_REQUESTED,
                AggregateType.REMINDER,
                stream_id,
                moment,
                version=1,
                summary=f"第 {spec.seq} 课提醒已排期（v{revision.plan_version}）",
                payload={
                    "reminder_id": reminder_id,
                    "participant_id": participant_id,
                    "plan_version": revision.plan_version,
                    "session_seq": spec.seq,
                    "remind_at": remind_at.isoformat(),
                    "scheduled_at": spec.scheduled_at,
                    "channel": channel,
                },
            )
            created.append(self.store.append(event))
        return created

    def reconcile_plan_change(
        self, participant_id: str, *, channel: str = "app"
    ) -> list[Event]:
        """计划调整后：作废被取代版本的待发提醒，再按新版本排期。"""
        revisions = self.plans.revisions(participant_id)
        if len(revisions) < 2:
            return self.schedule_for_plan(participant_id, channel=channel)
        latest = revisions[-1]
        changed: list[Event] = []
        for event in self.store.query(
            event_type=EventType.REMINDER_REQUESTED,
            aggregate_type=AggregateType.REMINDER,
        ):
            data = event.payload
            if data.get("participant_id") != participant_id:
                continue
            if int(data["plan_version"]) >= latest.plan_version:
                continue
            seq = int(data["session_seq"])
            if seq < latest.effective_from_seq:
                continue  # 生效点之前的课次不变，提醒也不变
            if self._terminal_state(event.aggregate_id):
                continue
            changed.append(self._cancel(
                event.aggregate_id, data,
                reason=f"计划调整为 v{latest.plan_version}，自第 "
                       f"{latest.effective_from_seq} 课起重新排期",
            ))
        changed.extend(self.schedule_for_plan(participant_id, channel=channel))
        return changed

    def cancel_pending(self, participant_id: str, *, reason: str) -> list[Event]:
        """作废某学员全部待发提醒（如安全暂停且课次时间需要重排）。"""
        cancelled: list[Event] = []
        for event in self.store.query(
            event_type=EventType.REMINDER_REQUESTED,
            aggregate_type=AggregateType.REMINDER,
        ):
            data = event.payload
            if data.get("participant_id") != participant_id:
                continue
            if self._terminal_state(event.aggregate_id):
                continue
            cancelled.append(self._cancel(event.aggregate_id, data, reason=reason))
        return cancelled

    # -- 投递 ---------------------------------------------------------------

    def pending(self, *, at: datetime | None = None) -> list[Reminder]:
        """重建当前待发队列：已请求、未投递、未取消、到期且未过期。"""
        moment = at or self.clock.now()
        completed: set[int] = set()
        # 只看该提醒对应参与者的完成课次
        pending: list[Reminder] = []
        for event in self.store.query(
            event_type=EventType.REMINDER_REQUESTED,
            aggregate_type=AggregateType.REMINDER,
        ):
            stream = self.store.stream(event.aggregate_id)
            if any(
                e.event_type in {
                    EventType.REMINDER_DELIVERED,
                    EventType.REMINDER_CANCELLED,
                }
                for e in stream
            ):
                continue
            data = event.payload
            participant_id = str(data["participant_id"])
            seq = int(data["session_seq"])
            if SafetyService.is_paused(self.store, participant_id):
                continue  # 暂停期不打扰，恢复后沿注入日历继续判断
            if seq in self.plans.completed_seqs(participant_id):
                continue  # 课已完成，提醒无意义
            remind_at = parse_time(str(data["remind_at"]))
            scheduled_at = parse_time(str(data["scheduled_at"]))
            if scheduled_at < moment:
                continue  # 课次时间已过（重启后不再补发过期提醒）
            if remind_at <= moment:
                pending.append(Reminder(
                    reminder_id=str(data["reminder_id"]),
                    participant_id=participant_id,
                    plan_version=int(data["plan_version"]),
                    session_seq=seq,
                    remind_at=remind_at,
                    scheduled_at=str(data["scheduled_at"]),
                    channel=str(data.get("channel", "app")),
                    stream_id=event.aggregate_id,
                ))
        pending.sort(key=lambda r: r.remind_at)
        return pending

    def cancel_stale(self) -> list[Event]:
        """把已过课次时间或课已完成的待发提醒正式作废（留痕，可解释）。"""
        moment = self.clock.now()
        stale: list[tuple[str, dict[str, Any], str]] = []
        for event in self.store.query(
            event_type=EventType.REMINDER_REQUESTED,
            aggregate_type=AggregateType.REMINDER,
        ):
            if self._terminal_state(event.aggregate_id):
                continue
            data = event.payload
            participant_id = str(data["participant_id"])
            seq = int(data["session_seq"])
            if seq in self.plans.completed_seqs(participant_id):
                stale.append((event.aggregate_id, data, "课次已完成"))
            elif parse_time(str(data["scheduled_at"])) < moment:
                stale.append((event.aggregate_id, data, "课次时间已过"))
        return [self._cancel(stream_id, data, reason=reason)
                for stream_id, data, reason in stale]

    def dispatch_due(self, *, limit: int | None = None) -> list[Reminder]:
        """投递到期提醒；每投递成功一条立即落 DELIVERED，重启不会重复发送。

        通道抛异常时该条不标记，下轮继续；后续条目本轮暂不处理，避免乱序。
        """
        due = self.pending()
        if limit is not None:
            due = due[:limit]
        delivered: list[Reminder] = []
        for reminder in due:
            if self.sender is not None:
                self.sender.send(reminder.message())
            self._mark_delivered(reminder)
            delivered.append(reminder)
        return delivered

    # -- 内部 ---------------------------------------------------------------

    def _has_active_reminder(self, participant_id: str, seq: int) -> bool:
        for event in self.store.query(
            event_type=EventType.REMINDER_REQUESTED,
            aggregate_type=AggregateType.REMINDER,
        ):
            data = event.payload
            if (data.get("participant_id") == participant_id
                    and int(data["session_seq"]) == seq
                    and not self._terminal_state(event.aggregate_id)):
                return True
        return False

    def _terminal_state(self, stream_id: str) -> bool:
        return any(
            e.event_type in {
                EventType.REMINDER_DELIVERED, EventType.REMINDER_CANCELLED
            }
            for e in self.store.stream(stream_id)
        )

    def _mark_delivered(self, reminder: Reminder) -> Event:
        version = self.store.version_of(reminder.stream_id) + 1
        event = make_event(
            EventType.REMINDER_DELIVERED,
            AggregateType.REMINDER,
            reminder.stream_id,
            self.clock.now(),
            version=version,
            summary=f"第 {reminder.session_seq} 课提醒已投递",
            payload={
                "reminder_id": reminder.reminder_id,
                "participant_id": reminder.participant_id,
                "plan_version": reminder.plan_version,
                "session_seq": reminder.session_seq,
                "channel": reminder.channel,
            },
        )
        return self.store.append(event, expected_version=version - 1)

    def _cancel(
        self, stream_id: str, request_payload: dict[str, Any], *, reason: str
    ) -> Event:
        version = self.store.version_of(stream_id) + 1
        event = make_event(
            EventType.REMINDER_CANCELLED,
            AggregateType.REMINDER,
            stream_id,
            self.clock.now(),
            version=version,
            summary=f"第 {request_payload['session_seq']} 课提醒已作废：{reason}",
            payload={
                "reminder_id": request_payload["reminder_id"],
                "participant_id": request_payload["participant_id"],
                "plan_version": request_payload["plan_version"],
                "session_seq": request_payload["session_seq"],
                "reason": reason,
            },
        )
        return self.store.append(event, expected_version=version - 1)
