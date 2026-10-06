"""可注入的时间来源与队列接口。

领域服务不直接读取系统时钟，而是依赖 Clock 抽象：测试和离线重放可注入
固定时钟，重启续处理时沿同一个日历坐标推进。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """返回带时区的当前时间。"""


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """始终返回同一时刻，用于测试与确定性重放。"""

    def __init__(self, moment: datetime | str) -> None:
        if isinstance(moment, str):
            moment = parse_time(moment)
        if moment.tzinfo is None:
            raise ValueError("固定时钟必须使用带时区的时间")
        self._moment = moment

    def now(self) -> datetime:
        return self._moment


class AdvancingClock:
    """固定起点，每次读取推进一个步长，模拟重启后的时间流逝。"""

    def __init__(self, start: datetime | str, seconds: float = 0.0) -> None:
        if isinstance(start, str):
            start = parse_time(start)
        if start.tzinfo is None:
            raise ValueError("起点必须使用带时区的时间")
        self._current = start
        self._seconds = seconds

    def now(self) -> datetime:
        moment = self._current
        from datetime import timedelta

        self._current = self._current + timedelta(seconds=self._seconds)
        return moment


def parse_time(value: str) -> datetime:
    """解析 ISO 8601；结尾 Z 视为 UTC；拒绝无时区时间。"""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"时间必须包含时区: {value}")
    return parsed


def as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)
