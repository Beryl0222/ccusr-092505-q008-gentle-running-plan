"""仅追加事件存储。

- 事件一旦写入不可修改、不可删除（已完成训练和原始体征不会因计划改写消失）。
- 每个聚合流的 ``version`` 从 1 递增，追加时做乐观并发校验。
- 全局 ``event_id`` 唯一：设备重发同一条上报时保持一次有效记录。
- JSONL 文件持久化，逐行 flush + fsync；重启后原样回放。
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .events import Event


class EventStoreError(RuntimeError):
    pass


class DuplicateEventError(EventStoreError):
    def __init__(self, event_id: str) -> None:
        super().__init__(f"事件已存在: {event_id}")
        self.event_id = event_id


class ConcurrentUpdateError(EventStoreError):
    def __init__(self, aggregate_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"聚合 {aggregate_id} 版本冲突: 期望 {expected}, 实际 {actual}"
        )
        self.aggregate_id = aggregate_id
        self.expected = expected
        self.actual = actual


class EventStore:
    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._global: list[Event] = []
        self._streams: dict[str, list[Event]] = defaultdict(list)
        self._ids: set[str] = set()
        if self._path is not None and self._path.exists():
            self._replay()

    def _replay(self) -> None:
        assert self._path is not None
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                event = Event.from_dict(json.loads(line))
                self._ingest(event)

    def _ingest(self, event: Event) -> None:
        if event.event_id in self._ids:
            raise DuplicateEventError(event.event_id)
        self._ids.add(event.event_id)
        stream = self._streams[event.aggregate_id]
        expected_position = len(stream) + 1
        if event.version != expected_position:
            raise EventStoreError(
                f"回放事件 {event.event_id} 版本不连续: "
                f"流 {event.aggregate_id} 期望 v{expected_position}, 收到 v{event.version}"
            )
        stream.append(event)
        self._global.append(event)

    # -- 写入 ---------------------------------------------------------------

    def append(
        self,
        event: Event,
        *,
        expected_version: int | None = None,
    ) -> Event:
        """追加事件。expected_version 为该流当前版本（新流为 0 或 None）。"""
        if event.event_id in self._ids:
            raise DuplicateEventError(event.event_id)
        stream = self._streams[event.aggregate_id]
        current = len(stream)
        if event.version != current + 1:
            raise ConcurrentUpdateError(event.aggregate_id, current, event.version)
        if expected_version is not None and expected_version != current:
            raise ConcurrentUpdateError(
                event.aggregate_id, expected_version, current
            )
        if self._path is not None:
            self._persist(event)
        self._ingest(event)
        return event

    def append_many(
        self,
        events: Iterable[Event],
        *,
        expected_version_by_aggregate: Mapping[str, int] | None = None,
    ) -> list[Event]:
        """整批提交：任一事件失败则全部不生效（调用方按片段重试）。"""
        events = list(events)
        expected = dict(expected_version_by_aggregate or {})
        staged: list[tuple[Event, int]] = []
        local_streams: dict[str, int] = defaultdict(int)
        seen: set[str] = set()
        for event in events:
            if event.event_id in self._ids or event.event_id in seen:
                raise DuplicateEventError(event.event_id)
            seen.add(event.event_id)
            aggregate_id = event.aggregate_id
            base = self.version_of(aggregate_id) + local_streams[aggregate_id]
            if event.version != base + 1:
                raise ConcurrentUpdateError(aggregate_id, base, event.version)
            if aggregate_id in expected and expected[aggregate_id] != self.version_of(aggregate_id):
                raise ConcurrentUpdateError(
                    aggregate_id, expected[aggregate_id], self.version_of(aggregate_id)
                )
            staged.append((event, base + 1))
            local_streams[aggregate_id] += 1
        if self._path is not None:
            for event, _ in staged:
                self._persist(event)
        for event, _ in staged:
            self._ingest(event)
        return [event for event, _ in staged]

    def _persist(self, event: Event) -> None:
        assert self._path is not None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
        # 同目录临时文件 + 原子替换用于整体重写场景；顺序追加直接写主文件。
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    # -- 读取 ---------------------------------------------------------------

    def stream(self, aggregate_id: str) -> list[Event]:
        return list(self._streams.get(aggregate_id, ()))

    def all_events(self) -> list[Event]:
        return list(self._global)

    def version_of(self, aggregate_id: str) -> int:
        return len(self._streams.get(aggregate_id, ()))

    def exists(self, event_id: str) -> bool:
        return event_id in self._ids

    def find(self, event_id: str) -> Event | None:
        for event in self._global:
            if event.event_id == event_id:
                return event
        return None

    def query(
        self,
        *,
        event_type: str | None = None,
        aggregate_type: str | None = None,
    ) -> list[Event]:
        result = self._global
        if event_type is not None:
            result = [e for e in result if e.event_type == event_type]
        if aggregate_type is not None:
            result = [e for e in result if e.aggregate_type == aggregate_type]
        return list(result)

    def snapshot_to(self, path: str | os.PathLike[str]) -> None:
        """将当前全部事件原子导出为 JSONL（用于备份/迁移）。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for event in self._global:
                    handle.write(
                        json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
                        + "\n"
                    )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            os.unlink(tmp_name)
            raise

    def payloads(self, aggregate_id: str) -> list[dict[str, Any]]:
        return [dict(e.payload) for e in self.stream(aggregate_id)]
