"""到期提醒调度：时钟与存储均可注入，服务重启后沿同一日历继续处理。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable


class ManualClock:
    """测试/联调用的可推进时钟；生产环境可替换为 time-based 实现。"""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now += timedelta(**kwargs)


@dataclass(frozen=True)
class Reminder:
    reminder_id: str
    participant_id: str
    session_index: int
    due_at: datetime
    message: str


class ReminderStore:
    """调度状态保存在注入的存储里；重启后用同一存储重建调度器即可续跑。"""

    def __init__(self) -> None:
        self.scheduled: dict[str, Reminder] = {}
        self.sent: list[str] = []


class ReminderScheduler:
    def __init__(self, clock: Callable[[], datetime], store: ReminderStore) -> None:
        self._clock = clock
        self._store = store

    def schedule(self, reminder: Reminder) -> None:
        """幂等登记：同一 reminder_id 重复调度不产生第二条。"""
        self._store.scheduled.setdefault(reminder.reminder_id, reminder)

    def process_due(self) -> list[Reminder]:
        """处理所有到期且未发送的提醒；每条只发送一次。"""
        now = self._clock()
        sent = set(self._store.sent)
        due = [
            reminder
            for reminder_id, reminder in sorted(self._store.scheduled.items())
            if reminder.due_at <= now and reminder_id not in sent
        ]
        for reminder in due:
            self._store.sent.append(reminder.reminder_id)
        return due
