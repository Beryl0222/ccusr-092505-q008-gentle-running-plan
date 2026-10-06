from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gentle_running_plan.views import AccessDenied

from support import enrolled_engine, make_plan


class PrivacyViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = enrolled_engine(
            "p1",
            profile_kwargs={
                "resting_sbp": 132,
                "resting_dbp": 86,
                "hypertension_controlled": True,
                "doctor_note": "患者私人体检备注原文",
            },
            sessions=make_plan(max_hr=84),
        )
        self.engine.roster.assign("coach-li", "p1")

    def test_coach_sees_minimal_training_info_only(self) -> None:
        view = self.engine.views.coach_participant_summary("coach-li", "p1")
        serialized = json.dumps(view, ensure_ascii=False)
        # 不暴露血压原值、医生备注原文
        self.assertNotIn("132", serialized)
        self.assertNotIn("私人体检备注原文", serialized)
        # 但执教所需的训练限制可见
        self.assertIn("training_limits", view)
        self.assertIn("max_heart_rate", view["training_limits"])

    def test_other_coach_denied(self) -> None:
        with self.assertRaises(AccessDenied):
            self.engine.views.coach_participant_summary("coach-wang", "p1")

    def test_participant_sees_pause_reason_and_next_session(self) -> None:
        self.engine.ingest.log_report("p1", {
            "report_id": "r1",
            "observed_at": "2026-10-07T09:00:00+08:00",
            "session_seq": 1,
            "readings": {"peak_heart_rate": 88},
            "symptoms": ["knee_locking"],
        })
        view = self.engine.views.participant_view("p1", "p1")
        self.assertTrue(view["paused"])
        self.assertIn("膝", view["pause"]["reason"])
        self.assertEqual(
            "需由专业人员完成新的复核后方可恢复训练",
            view["pause"]["resume_condition"],
        )
        self.assertTrue(view["next_session"]["on_hold"])

    def test_participant_cannot_view_others(self) -> None:
        with self.assertRaises(AccessDenied):
            self.engine.views.participant_view("p1", "p2")

    def test_dashboard_only_lists_own_students(self) -> None:
        from gentle_running_plan import HealthProfile

        # 同一站点内另一位教练的学员
        self.engine.screening.submit(HealthProfile(participant_id="p2", age=60))
        self.engine.plans.publish("p2", make_plan(max_hr=96))
        self.engine.roster.assign("coach-wang", "p2")
        li_view = self.engine.views.coach_dashboard("coach-li")
        wang_view = self.engine.views.coach_dashboard("coach-wang")
        self.assertEqual(["p1"], [row["participant_id"] for row in li_view])
        self.assertEqual(["p2"], [row["participant_id"] for row in wang_view])

    def test_roster_persistence_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "roster.json"
            self.engine.roster.save(path)
            from gentle_running_plan.views import Roster

            reloaded = Roster.load(path)
            self.assertTrue(reloaded.is_coach_of("coach-li", "p1"))
            self.assertFalse(reloaded.is_coach_of("coach-li", "p2"))


if __name__ == "__main__":
    unittest.main()
