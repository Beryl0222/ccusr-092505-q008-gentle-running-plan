from __future__ import annotations

import unittest

from gentle_running_plan.events import EventType
from gentle_running_plan.plan import PlanError, validate_sessions

from support import enrolled_engine, make_engine, make_plan


class PlanVersioningTests(unittest.TestCase):
    def test_publish_requires_approved_screening(self) -> None:
        engine = make_engine()
        with self.assertRaises(PlanError):
            engine.plans.publish("p1", make_plan())

    def test_plan_exceeding_authorization_is_rejected(self) -> None:
        engine = enrolled_engine()
        too_long = make_plan()
        # 25 分钟课（5+5+ 10 分钟间歇）超过 20 分钟授权上限
        from gentle_running_plan import Interval, SessionSpec

        long_session = SessionSpec(
            seq=1,
            scheduled_at="2026-10-07T08:00:00+08:00",
            warmup_minutes=5,
            intervals=(Interval("walk", 900, 1),),
            cooldown_minutes=5,
            targets={"max_heart_rate": 91, "max_rpe": 3},
        )
        violations = validate_sessions(
            [long_session],
            engine.plans._latest_authorization("p1"),
        )
        self.assertTrue(any(v.code == "session_too_long" for v in violations))

    def test_adjustment_only_takes_effect_on_future_sessions(self) -> None:
        engine = enrolled_engine()
        original = make_plan()
        # 完成第 1 课
        engine.ingest.log_report("p1", {
            "report_id": "r1",
            "observed_at": "2026-10-07T09:00:00+08:00",
            "session_seq": 1,
            "readings": {"peak_heart_rate": 88, "rpe": 2},
        })
        # 手工构造后续课：1、2 课保持不变，3、4 课缩短慢跑段并下调心率目标
        from gentle_running_plan import Interval, SessionSpec

        new_sessions = list(original[:2]) + [
            SessionSpec(
                seq=3, scheduled_at="2026-10-11T08:00:00+08:00",
                warmup_minutes=5,
                intervals=(Interval("walk", 120, 3), Interval("slow_jog", 30, 3)),
                cooldown_minutes=5,
                targets={"max_heart_rate": 88, "max_rpe": 3},
            ),
            SessionSpec(
                seq=4, scheduled_at="2026-10-13T08:00:00+08:00",
                warmup_minutes=5,
                intervals=(Interval("walk", 120, 3), Interval("slow_jog", 30, 3)),
                cooldown_minutes=5,
                targets={"max_heart_rate": 88, "max_rpe": 3},
            ),
        ]
        event = engine.plans.adjust(
            "p1", new_sessions, effective_from_seq=3,
            reason="训练后膝部酸胀，缩短慢跑段",
        )
        self.assertEqual(EventType.PLAN_ADJUSTED, event.event_type)
        # 第 1、2 课仍按 v1，第 3 课按 v2
        self.assertEqual(
            engine.ingest._effective_plan_version("p1", 1), 1
        )
        self.assertEqual(
            engine.ingest._effective_plan_version("p1", 2), 1
        )
        self.assertEqual(
            engine.ingest._effective_plan_version("p1", 3), 2
        )
        spec3 = engine.plans.effective_session("p1", 3)
        self.assertEqual(30, spec3.intervals[-1].seconds)

    def test_adjustment_cannot_rewrite_completed_session(self) -> None:
        engine = enrolled_engine()
        engine.ingest.log_report("p1", {
            "report_id": "r1",
            "observed_at": "2026-10-07T09:00:00+08:00",
            "session_seq": 1,
            "readings": {"peak_heart_rate": 88},
        })
        from gentle_running_plan import Interval

        sessions = make_plan()
        sessions[0] = type(sessions[0])(
            seq=1,
            scheduled_at=sessions[0].scheduled_at,
            warmup_minutes=5,
            intervals=(Interval("walk", 120, 3), Interval("slow_jog", 15, 3)),
            cooldown_minutes=5,
            targets={"max_heart_rate": 91, "max_rpe": 3},
        )
        with self.assertRaises(PlanError):
            engine.plans.adjust(
                "p1", sessions, effective_from_seq=1, reason="试图改写已完成课"
            )

    def test_past_sessions_must_remain_byte_identical(self) -> None:
        engine = enrolled_engine()
        sessions = make_plan()
        # 未完成任何课，但生效点设为 3，却偷偷改了第 2 课
        from gentle_running_plan import Interval

        sessions[1] = type(sessions[1])(
            seq=2,
            scheduled_at=sessions[1].scheduled_at,
            warmup_minutes=5,
            intervals=(Interval("walk", 120, 3), Interval("slow_jog", 30, 3)),
            cooldown_minutes=5,
            targets={"max_heart_rate": 91, "max_rpe": 3},
        )
        with self.assertRaises(PlanError):
            engine.plans.adjust(
                "p1", sessions, effective_from_seq=3, reason="夹带改动"
            )

    def test_adjustment_requires_reason(self) -> None:
        engine = enrolled_engine()
        with self.assertRaises(PlanError):
            engine.plans.adjust(
                "p1", make_plan(), effective_from_seq=2, reason="   "
            )

    def test_session_sequence_must_start_at_one(self) -> None:
        engine = enrolled_engine()
        sessions = make_plan()[1:]
        with self.assertRaises(PlanError):
            engine.plans.publish("p2", sessions)

    def test_completed_sessions_survive_plan_rewrite(self) -> None:
        engine = enrolled_engine()
        engine.ingest.log_report("p1", {
            "report_id": "r1",
            "observed_at": "2026-10-07T09:00:00+08:00",
            "session_seq": 1,
            "readings": {"peak_heart_rate": 88, "rpe": 2, "distance_km": 1.2},
        })
        logged = engine.store.query(event_type=EventType.SESSION_LOGGED)
        self.assertEqual(1, len(logged))
        self.assertEqual(
            1.2, logged[0].payload["readings"]["distance_km"]
        )
        self.assertEqual({1}, engine.plans.completed_seqs("p1"))


if __name__ == "__main__":
    unittest.main()
