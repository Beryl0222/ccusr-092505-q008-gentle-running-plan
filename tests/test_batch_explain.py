from __future__ import annotations

import unittest

from support import enrolled_engine, make_engine, make_plan

from gentle_running_plan.batch import PlanFragment
from gentle_running_plan.explain import ExplanationService


class BatchTests(unittest.TestCase):
    def test_fragment_failure_is_isolated_and_retryable(self) -> None:
        engine = make_engine()
        engine.screening.submit(__import__(
            "gentle_running_plan", fromlist=["HealthProfile"]
        ).HealthProfile(participant_id="a", age=60))
        engine.screening.submit(__import__(
            "gentle_running_plan", fromlist=["HealthProfile"]
        ).HealthProfile(participant_id="b", age=60))

        fragments = [
            PlanFragment("f-a", "a", make_plan(n=2)),
            PlanFragment("f-b", "b", make_plan(n=2)),
            PlanFragment("f-c", "c", make_plan(n=2)),  # 未筛查
        ]
        result = engine.batch.generate(fragments)
        self.assertFalse(result.all_succeeded)
        self.assertEqual({"f-a", "f-b"}, {o.fragment_id for o in result.succeeded})
        self.assertEqual(["f-c"], [o.fragment_id for o in result.failed])

        # 立即重试：c 仍失败，a/b 没有被重复处理
        again = engine.batch.retry(result)
        self.assertEqual(["f-c"], [o.fragment_id for o in again.failed])
        self.assertEqual(1, len(engine.plans.revisions("a")))

        # 补齐筛查后重试 c：全部成功
        engine.screening.submit(__import__(
            "gentle_running_plan", fromlist=["HealthProfile"]
        ).HealthProfile(participant_id="c", age=60))
        final = engine.batch.retry(again)
        self.assertTrue(final.all_succeeded)
        self.assertEqual(1, len(engine.plans.revisions("c")))

    def test_retry_can_override_invalid_sessions(self) -> None:
        engine = make_engine()
        from gentle_running_plan import HealthProfile, Interval, SessionSpec

        engine.screening.submit(HealthProfile(participant_id="a", age=68))
        too_long = [SessionSpec(
            seq=1, scheduled_at="2026-10-08T08:00:00+08:00",
            warmup_minutes=5,
            intervals=(Interval("walk", 900, 1),),
            cooldown_minutes=5,
            targets={"max_heart_rate": 91, "max_rpe": 3},
        )]
        result = engine.batch.generate([PlanFragment("f-a", "a", too_long)])
        self.assertFalse(result.all_succeeded)
        fixed = engine.batch.retry(
            result, overrides={"f-a": make_plan(n=2)}
        )
        self.assertTrue(fixed.all_succeeded)


class ExplanationTests(unittest.TestCase):
    def test_timeline_links_execution_thresholds_and_changes(self) -> None:
        engine = enrolled_engine(now="2026-10-07T09:05:00+08:00")
        engine.ingest.log_report("p1", {
            "report_id": "r1",
            "observed_at": "2026-10-07T09:00:00+08:00",
            "session_seq": 1,
            "readings": {"peak_heart_rate": 140},
        })
        cleared = engine.screening.record_review(
            "p1", reviewer_id="doc1", verdict="restricted",
            note="降强度", profile_updates={"hypertension_controlled": True},
            clear_pause=True,
        )
        engine.safety.resume_after_review("p1", review_event_id=cleared.event_id)
        from gentle_running_plan import Interval, SessionSpec

        new_sessions = make_plan()[:2] + [
            SessionSpec(
                seq=i + 1,
                scheduled_at=f"2026-10-{7 + i * 2:02d}T08:00:00+08:00",
                warmup_minutes=5,
                intervals=(Interval("walk", 120, 3), Interval("slow_jog", 30, 3)),
                cooldown_minutes=5,
                targets={"max_heart_rate": 84, "max_rpe": 3},
            )
            for i in range(2, 4)
        ]
        engine.plans.adjust(
            "p1", new_sessions, effective_from_seq=3, reason="心率越限后降量"
        )

        explain = ExplanationService(engine.store)
        kinds = [item.kind for item in explain.timeline("p1")]
        self.assertIn("screening", kinds)
        self.assertIn("session_logged", kinds)
        self.assertIn("safety_paused", kinds)
        self.assertIn("review", kinds)
        self.assertIn("plan_resumed", kinds)
        self.assertIn("plan_adjusted", kinds)

        chain3 = explain.session_chain("p1", 3)
        self.assertEqual(2, chain3["plan_version"])
        self.assertEqual(3, chain3["effective_from_seq"])
        self.assertEqual(84, chain3["thresholds"]["max_heart_rate"])

        chain1 = explain.session_chain("p1", 1)
        self.assertEqual(1, chain1["plan_version"])
        # 暂停发生在第 1 课计划时间之后
        self.assertFalse(chain1["paused_when_due"])

        report = explain.change_report("p1")
        self.assertEqual([1, 2], [r["plan_version"] for r in report])
        self.assertEqual(3, report[1]["effective_from_seq"])
        self.assertEqual("心率越限后降量", report[1]["reason"])


if __name__ == "__main__":
    unittest.main()
