from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gentle_running_plan import (
    FixedClock,
    Interval,
    PrescriptionEngine,
    SessionSpec,
)
from gentle_running_plan.clock import AdvancingClock
from gentle_running_plan.events import EventType
from gentle_running_plan.store import EventStore

from support import enrolled_engine, make_plan


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.fail_times = 0

    def send(self, message: dict) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("通道暂时不可用")
        self.sent.append(message)


class ReminderTests(unittest.TestCase):
    def test_due_reminder_delivered_once(self) -> None:
        engine = enrolled_engine()
        engine.reminders.schedule_for_plan("p1")
        # 第 1 课 10-07 08:00，默认提前 60 分钟；推进到 07:05
        engine.clock = AdvancingClock("2026-10-07T07:05:00+08:00")
        engine.reminders.clock = engine.clock
        sender = RecordingSender()
        engine.reminders.sender = sender
        delivered = engine.reminders.dispatch_due()
        self.assertEqual([1], [r.session_seq for r in delivered])
        engine.reminders.dispatch_due()
        self.assertEqual(1, len(sender.sent))

    def test_restart_continues_due_processing(self) -> None:
        engine = enrolled_engine()
        engine.reminders.schedule_for_plan("p1")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            engine.store.snapshot_to(path)

            store = EventStore(path)
            sender = RecordingSender()
            restarted = PrescriptionEngine(
                store,
                clock=AdvancingClock("2026-10-09T07:30:00+08:00"),
                sender=sender,
            )
            delivered = restarted.reminders.dispatch_due()
            # 第 1 课时间已过不补发；第 2 课（10-09 08:00）提醒到期
            self.assertEqual([2], [r.session_seq for r in delivered])
            self.assertEqual(1, len(sender.sent))
            # 再重启一次：第 2 课不重复投递，事件已持久化
            store2 = EventStore(path)
            sender2 = RecordingSender()
            again = PrescriptionEngine(
                store2,
                clock=AdvancingClock("2026-10-09T07:45:00+08:00"),
                sender=sender2,
            )
            again.reminders.dispatch_due()
            self.assertEqual(0, len(sender2.sent))

    def test_failed_channel_does_not_mark_delivered(self) -> None:
        engine = enrolled_engine()
        engine.reminders.schedule_for_plan("p1")
        engine.clock = AdvancingClock("2026-10-07T07:05:00+08:00")
        engine.reminders.clock = engine.clock
        sender = RecordingSender()
        sender.fail_times = 1
        engine.reminders.sender = sender
        with self.assertRaises(RuntimeError):
            engine.reminders.dispatch_due()
        # 未落 DELIVERED：下轮仍可投递
        sender.fail_times = 0
        delivered = engine.reminders.dispatch_due()
        self.assertEqual(1, len(delivered))

    def test_paused_participant_gets_no_reminder(self) -> None:
        engine = enrolled_engine()
        engine.reminders.schedule_for_plan("p1")
        engine.ingest.log_report("p1", {
            "report_id": "r1",
            "observed_at": "2026-10-07T07:40:00+08:00",
            "session_seq": 1,
            "readings": {"peak_heart_rate": 88},
            "symptoms": ["chest_pain"],
        })
        engine.clock = AdvancingClock("2026-10-07T07:45:00+08:00")
        engine.reminders.clock = engine.clock
        self.assertEqual([], engine.reminders.pending())

    def test_plan_change_cancels_old_and_reschedules_future_only(self) -> None:
        engine = enrolled_engine(now="2026-10-06T09:00:00+08:00")
        engine.reminders.schedule_for_plan("p1")
        # 完成第 1、2 课
        for seq, day in ((1, 7), (2, 9)):
            engine.ingest.log_report("p1", {
                "report_id": f"r{seq}",
                "observed_at": f"2026-10-{day:02d}T09:00:00+08:00",
                "session_seq": seq,
                "readings": {"peak_heart_rate": 88},
            })
        new_sessions = make_plan()[:2] + [
            SessionSpec(
                seq=seq,
                scheduled_at=f"2026-10-{7 + (seq - 1) * 2:02d}T08:00:00+08:00",
                warmup_minutes=5,
                intervals=(Interval("walk", 120, 3), Interval("slow_jog", 30, 3)),
                cooldown_minutes=5,
                targets={"max_heart_rate": 91, "max_rpe": 3},
            )
            for seq in (3, 4)
        ]
        engine.plans.adjust(
            "p1", new_sessions, effective_from_seq=3, reason="缩短慢跑段"
        )
        changed = engine.reminders.reconcile_plan_change("p1")
        cancelled = [e for e in changed if e.event_type == EventType.REMINDER_CANCELLED]
        requested = [e for e in changed if e.event_type == EventType.REMINDER_REQUESTED]
        # 只作废并重排受影响的第 3、4 课
        self.assertEqual({3, 4}, {e.payload["session_seq"] for e in cancelled})
        self.assertEqual({3, 4}, {e.payload["session_seq"] for e in requested})
        self.assertTrue(all(e.payload["plan_version"] == 2 for e in requested))


    def test_reschedule_is_idempotent(self) -> None:
        engine = enrolled_engine()
        first = engine.reminders.schedule_for_plan("p1")
        second = engine.reminders.schedule_for_plan("p1")
        self.assertEqual(4, len(first))
        self.assertEqual([], second)

    def test_file_backed_appends_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            engine = PrescriptionEngine(
                path, clock=FixedClock("2026-10-06T08:00:00+08:00")
            )
            from gentle_running_plan import HealthProfile

            engine.screening.submit(HealthProfile(participant_id="p1", age=68))
            engine.plans.publish("p1", make_plan(n=1))
            engine.reminders.schedule_for_plan("p1")

            reloaded = PrescriptionEngine(
                EventStore(path),
                clock=AdvancingClock("2026-10-07T07:30:00+08:00"),
            )
            delivered = reloaded.reminders.dispatch_due()
            self.assertEqual([1], [r.session_seq for r in delivered])
            # 第三次打开同一文件，投递不重复
            again = PrescriptionEngine(
                EventStore(path),
                clock=AdvancingClock("2026-10-07T07:45:00+08:00"),
            )
            self.assertEqual([], again.reminders.dispatch_due())


if __name__ == "__main__":
    unittest.main()
