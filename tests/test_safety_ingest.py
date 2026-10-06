from __future__ import annotations

import unittest

from gentle_running_plan.events import EventType
from gentle_running_plan.ingest import IngestStatus
from gentle_running_plan.rules import MedicalVerdict
from gentle_running_plan.safety import SafetyError

from support import enrolled_engine, make_engine, make_plan


def _report(
    report_id: str,
    *,
    seq: int | None = 1,
    observed: str = "2026-10-07T09:00:00+08:00",
    readings=None,
    symptoms=(),
    source: str = "watch",
) -> dict:
    return {
        "report_id": report_id,
        "session_seq": seq,
        "observed_at": observed,
        "readings": readings or {"peak_heart_rate": 88, "rpe": 2},
        "symptoms": list(symptoms),
        "source": source,
    }


class IngestTests(unittest.TestCase):
    def test_first_report_is_logged(self) -> None:
        engine = enrolled_engine()
        result = engine.ingest.log_report("p1", _report("r1"))
        self.assertEqual(IngestStatus.LOGGED, result.status)
        self.assertIsNotNone(result.event)
        self.assertTrue(result.event.payload["counts_toward_plan"])

    def test_replayed_report_keeps_single_record(self) -> None:
        engine = enrolled_engine()
        first = engine.ingest.log_report("p1", _report("r1"))
        replay = engine.ingest.log_report("p1", _report("r1"))
        self.assertEqual(IngestStatus.DUPLICATE_EXACT, replay.status)
        self.assertEqual(first.event.event_id, replay.event.event_id)
        logged = engine.store.query(event_type=EventType.SESSION_LOGGED)
        self.assertEqual(1, len(logged))

    def test_same_slot_equal_values_is_slot_duplicate(self) -> None:
        engine = enrolled_engine()
        engine.ingest.log_report("p1", _report("phone-1", source="phone"))
        result = engine.ingest.log_report(
            "p1", _report("coach-1", readings={"peak_heart_rate": 89, "rpe": 2},
                           source="coach_sheet")
        )
        self.assertEqual(IngestStatus.DUPLICATE_SLOT, result.status)
        self.assertEqual(1, len(
            engine.store.query(event_type=EventType.SESSION_LOGGED)
        ))

    def test_conflicting_values_go_to_review_never_average(self) -> None:
        engine = enrolled_engine()
        engine.ingest.log_report(
            "p1", _report("phone-1", readings={"peak_heart_rate": 88})
        )
        result = engine.ingest.log_report(
            "p1", _report("coach-1", readings={"peak_heart_rate": 120})
        )
        self.assertEqual(IngestStatus.CONFLICTED, result.status)
        conflict_events = engine.store.query(
            event_type=EventType.REPORT_CONFLICTED
        )
        self.assertEqual(1, len(conflict_events))
        payload = conflict_events[0].payload
        self.assertEqual("pending_review", payload["resolution"])
        self.assertEqual(88, payload["conflicts"][0]["kept"])
        self.assertEqual(120, payload["conflicts"][0]["incoming"])
        # 有效记录仍是先到的一条，没有产生 104 的平均值
        logged = engine.store.query(event_type=EventType.SESSION_LOGGED)
        self.assertEqual(1, len(logged))
        self.assertEqual(88, logged[0].payload["readings"]["peak_heart_rate"])

    def test_extra_symptom_in_late_report_is_conflict(self) -> None:
        engine = enrolled_engine()
        engine.ingest.log_report("p1", _report("r1"))
        result = engine.ingest.log_report(
            "p1", _report("r2", symptoms=["knee_locking"])
        )
        self.assertEqual(IngestStatus.CONFLICTED, result.status)
        self.assertIn("symptoms", [c["metric"] for c in result.conflicts])

    def test_offline_late_arrival_is_still_logged(self) -> None:
        engine = enrolled_engine()
        result = engine.ingest.log_report(
            "p1",
            _report("r1", observed="2026-10-07T09:00:00+08:00"),
            received_at="2026-10-09T20:00:00+08:00",
        )
        self.assertEqual(IngestStatus.LOGGED, result.status)
        self.assertTrue(result.event.payload["late"])

    def test_adhoc_session_uses_day_slot(self) -> None:
        engine = enrolled_engine()
        engine.ingest.log_report(
            "p1", _report("r1", seq=None, observed="2026-10-20T07:00:00+08:00")
        )
        result = engine.ingest.log_report(
            "p1", _report("r2", seq=None, observed="2026-10-20T19:00:00+08:00",
                          readings={"peak_heart_rate": 130})
        )
        self.assertEqual(IngestStatus.CONFLICTED, result.status)
        result2 = engine.ingest.log_report(
            "p1", _report("r3", seq=None, observed="2026-10-21T07:00:00+08:00")
        )
        self.assertEqual(IngestStatus.LOGGED, result2.status)


class SafetyTests(unittest.TestCase):
    def test_red_flag_pauses_immediately(self) -> None:
        engine = enrolled_engine()
        result = engine.ingest.log_report(
            "p1", _report("r1", symptoms=["chest_pain"])
        )
        self.assertTrue(result.paused)
        self.assertTrue(engine.safety.is_paused(engine.store, "p1"))
        pauses = engine.store.query(event_type=EventType.SAFETY_PAUSED)
        self.assertEqual(1, len(pauses))
        self.assertEqual("red_flag", pauses[0].payload["trigger"])

    def test_threshold_breach_pauses(self) -> None:
        engine = enrolled_engine()
        result = engine.ingest.log_report(
            "p1", _report("r1", readings={"peak_heart_rate": 150})
        )
        self.assertTrue(result.paused)
        pause = engine.store.query(
            event_type=EventType.SAFETY_PAUSED
        )[0].payload
        self.assertEqual("threshold_breach", pause["trigger"])
        self.assertEqual("AUTH-HR", pause["breaches"][0]["rule"])

    def test_publish_and_adjust_blocked_while_paused(self) -> None:
        engine = enrolled_engine()
        engine.ingest.log_report(
            "p1", _report("r1", symptoms=["dizziness"])
        )
        from gentle_running_plan.plan import PlanError

        with self.assertRaises(PlanError):
            engine.plans.adjust(
                "p1", make_plan(), effective_from_seq=2, reason="暂停中调整"
            )

    def test_activity_during_pause_is_held_for_review(self) -> None:
        engine = enrolled_engine()
        engine.ingest.log_report(
            "p1", _report("r1", symptoms=["chest_pain"])
        )
        result = engine.ingest.log_report(
            "p1", _report("r2", seq=2, observed="2026-10-09T09:00:00+08:00")
        )
        self.assertEqual(IngestStatus.REJECTED_PAUSED, result.status)
        # 不计完成
        self.assertNotIn(2, engine.plans.completed_seqs("p1"))
        conflicted = engine.store.query(
            event_type=EventType.REPORT_CONFLICTED
        )
        self.assertTrue(any(
            e.payload["conflict_type"] == "paused_window_activity"
            for e in conflicted
        ))

    def test_old_review_cannot_resume(self) -> None:
        engine = enrolled_engine()
        # 暂停前先有一次通过的复核（不能用于恢复）
        old = engine.screening.record_review(
            "p1", reviewer_id="doc1", verdict="approved", note="基线复核"
        )
        engine.ingest.log_report(
            "p1", _report("r1", symptoms=["chest_pain"],
                          observed="2026-10-07T09:00:00+08:00")
        )
        with self.assertRaises(SafetyError):
            engine.safety.resume_after_review(
                "p1", review_event_id=old.event_id
            )

    def test_only_new_clearance_resumes(self) -> None:
        engine = enrolled_engine()
        engine.ingest.log_report(
            "p1", _report("r1", symptoms=["chest_pain"])
        )
        # 结论仍为拒绝的复核不能恢复
        rejected = engine.screening.record_review(
            "p1", reviewer_id="doc1", verdict="rejected", note="继续观察"
        )
        with self.assertRaises(SafetyError):
            engine.safety.resume_after_review(
                "p1", review_event_id=rejected.event_id
            )
        cleared = engine.screening.record_review(
            "p1", reviewer_id="doc1", verdict="restricted",
            note="症状排除，降强度恢复",
            profile_updates={"hypertension_controlled": True},
            clear_pause=True,
        )
        resume = engine.safety.resume_after_review(
            "p1", review_event_id=cleared.event_id
        )
        self.assertEqual(EventType.PLAN_RESUMED, resume.event_type)
        self.assertFalse(engine.safety.is_paused(engine.store, "p1"))
        # 暂停事实仍在
        self.assertEqual(1, len(
            engine.store.query(event_type=EventType.SAFETY_PAUSED)
        ))

    def test_resume_requires_clear_pause_flag(self) -> None:
        engine = enrolled_engine()
        engine.ingest.log_report(
            "p1", _report("r1", symptoms=["chest_pain"])
        )
        cleared = engine.screening.record_review(
            "p1", reviewer_id="doc1", verdict="approved",
            note="未声明解除暂停",
        )
        with self.assertRaises(SafetyError):
            engine.safety.resume_after_review(
                "p1", review_event_id=cleared.event_id
            )

    def test_unknown_red_flag_code_rejected(self) -> None:
        engine = enrolled_engine()
        with self.assertRaises(ValueError):
            engine.ingest.log_report(
                "p1", _report("r1", symptoms=["not_a_real_flag"])
            )

    def test_doctor_rejected_screening_cannot_publish(self) -> None:
        engine = make_engine()
        from gentle_running_plan import HealthProfile
        from gentle_running_plan.rules import MedicalVerdict

        engine.screening.submit(HealthProfile(
            participant_id="p9", age=60,
            doctor_verdict=MedicalVerdict.REJECTED,
        ))
        from gentle_running_plan.plan import PlanError

        with self.assertRaises(PlanError):
            engine.plans.publish("p9", make_plan())

    def test_repeated_pause_resume_cycles_require_fresh_review_each_time(self) -> None:
        engine = enrolled_engine()

        # 第一轮暂停/恢复
        engine.ingest.log_report(
            "p1", _report("r1", symptoms=["dizziness"])
        )
        self.assertTrue(engine.safety.is_paused(engine.store, "p1"))
        c1 = engine.screening.record_review(
            "p1", reviewer_id="doc1", verdict="approved",
            note="首次解除", clear_pause=True,
        )
        engine.safety.resume_after_review("p1", review_event_id=c1.event_id)
        self.assertFalse(engine.safety.is_paused(engine.store, "p1"))

        # 第二次暂停：第一次的复核不能再恢复
        engine.ingest.log_report(
            "p1", _report(
                "r2", seq=2, observed="2026-10-09T09:00:00+08:00",
                symptoms=["knee_locking"],
            )
        )
        self.assertTrue(engine.safety.is_paused(engine.store, "p1"))
        with self.assertRaises(SafetyError):
            engine.safety.resume_after_review("p1", review_event_id=c1.event_id)
        c2 = engine.screening.record_review(
            "p1", reviewer_id="doc1", verdict="restricted",
            note="再次复核通过", clear_pause=True,
        )
        engine.safety.resume_after_review("p1", review_event_id=c2.event_id)
        self.assertFalse(engine.safety.is_paused(engine.store, "p1"))
        # 两次暂停事实都保留
        self.assertEqual(2, len(
            engine.store.query(event_type=EventType.SAFETY_PAUSED)
        ))


if __name__ == "__main__":
    unittest.main()
