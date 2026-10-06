from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gentle_running_plan.clock import parse_time
from gentle_running_plan.events import AggregateType, EventType, make_event
from gentle_running_plan.store import (
    ConcurrentUpdateError,
    DuplicateEventError,
    EventStore,
)


def _event(event_id: str, stream: str = "plan:p1", version: int = 1) -> object:
    return make_event(
        EventType.PLAN_PUBLISHED,
        AggregateType.EXERCISE_PLAN,
        stream,
        "2026-10-06T08:00:00+08:00",
        version=version,
        summary="测试事件",
        event_id=event_id,
    )


class EventStoreTests(unittest.TestCase):
    def test_version_must_be_sequential(self) -> None:
        store = EventStore()
        store.append(_event("e1", version=1))
        with self.assertRaises(ConcurrentUpdateError):
            store.append(_event("e2", version=3))
        store.append(_event("e2", version=2), expected_version=1)
        self.assertEqual(2, store.version_of("plan:p1"))

    def test_duplicate_event_id_rejected(self) -> None:
        store = EventStore()
        store.append(_event("e1"))
        with self.assertRaises(DuplicateEventError):
            store.append(_event("e1", stream="plan:p2"))

    def test_expected_version_guards_concurrency(self) -> None:
        store = EventStore()
        store.append(_event("e1"))
        with self.assertRaises(ConcurrentUpdateError):
            store.append(_event("e2", version=2), expected_version=0)

    def test_append_many_is_atomic(self) -> None:
        store = EventStore()
        store.append(_event("e1"))
        with self.assertRaises(DuplicateEventError):
            store.append_many([
                _event("e2", version=2),
                _event("e1", version=3),
            ])
        self.assertEqual(1, store.version_of("plan:p1"))

    def test_events_survive_restart_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            store = EventStore(path)
            store.append(_event("e1"))
            store.append(_event("e2", version=2))
            store.append(make_event(
                EventType.SESSION_LOGGED,
                AggregateType.TRAINING_SESSION,
                "session:p1:1",
                parse_time("2026-10-07T09:00:00+08:00"),
                version=1,
                summary="训练记录",
                event_id="s1",
                payload={"readings": {"peak_heart_rate": 88}},
            ))
            reloaded = EventStore(path)
            self.assertEqual(3, len(reloaded.all_events()))
            self.assertEqual(2, reloaded.version_of("plan:p1"))
            self.assertEqual(
                88,
                reloaded.find("s1").payload["readings"]["peak_heart_rate"],
            )
            # 重放时同样拒绝重复 id
            with self.assertRaises(DuplicateEventError):
                reloaded.append(_event("e1"))


if __name__ == "__main__":
    unittest.main()
