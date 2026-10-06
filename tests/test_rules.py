from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gentle_running_plan.rules import (
    BP_BLOCK_DBP,
    BP_BLOCK_SBP,
    Footwear,
    HealthProfile,
    KneeStatus,
    MedicalVerdict,
    RehabStatus,
    Surface,
    derive_authorization,
)


def profile(**kwargs) -> HealthProfile:
    base = {"participant_id": "p1", "age": 68}
    base.update(kwargs)
    return HealthProfile(**base)


class RuleEngineTests(unittest.TestCase):
    def test_senior_beginner_baseline(self) -> None:
        auth = derive_authorization(profile())
        self.assertTrue(auth.authorized)
        # HRmax=152, 60% = 91.2 -> 91
        self.assertEqual(auth.max_heart_rate, 91)
        self.assertEqual(auth.max_rpe, 3)
        self.assertTrue(auth.supervision_required)
        self.assertGreaterEqual(auth.supervised_sessions, 4)
        self.assertIn("slow_jog", auth.allowed_modes)
        rule_ids = {d.rule_id for d in auth.basis}
        self.assertIn("R-BASE", rule_ids)
        self.assertIn("R-SUPERVISE-1", rule_ids)

    def test_doctor_rejection_blocks(self) -> None:
        auth = derive_authorization(
            profile(doctor_verdict=MedicalVerdict.REJECTED)
        )
        self.assertFalse(auth.authorized)
        self.assertIn("R-DOCTOR-1", {d.rule_id for d in auth.basis})
        # 阻断态不输出可能被误读的运动参数
        self.assertIsNone(auth.max_heart_rate)

    def test_doctor_restriction_tightens(self) -> None:
        auth = derive_authorization(profile(
            doctor_verdict=MedicalVerdict.RESTRICTED,
            doctor_overrides={"max_heart_rate": 80, "no_jogging": True},
        ))
        self.assertTrue(auth.authorized)
        self.assertEqual(auth.max_heart_rate, 80)
        self.assertNotIn("slow_jog", auth.allowed_modes)
        self.assertEqual(auth.max_jog_bout_seconds, 0)

    def test_high_blood_pressure_blocks(self) -> None:
        auth = derive_authorization(
            profile(resting_sbp=BP_BLOCK_SBP, resting_dbp=90)
        )
        self.assertFalse(auth.authorized)
        self.assertTrue(
            any("血压" in r for r in auth.review_reasons)
        )
        auth2 = derive_authorization(
            profile(resting_sbp=120, resting_dbp=BP_BLOCK_DBP)
        )
        self.assertFalse(auth2.authorized)

    def test_controlled_hypertension_lowers_hr_and_adds_flags(self) -> None:
        auth = derive_authorization(
            profile(hypertension_controlled=True)
        )
        # HRmax=152, 55% = 83.6 -> 84
        self.assertEqual(auth.max_heart_rate, 84)
        self.assertTrue(any("血压" in f or "头痛" in f for f in auth.red_flags))

    def test_active_knee_blocks_history_knee_restricts(self) -> None:
        active = derive_authorization(profile(knee=KneeStatus.ACTIVE))
        self.assertFalse(active.authorized)
        history = derive_authorization(profile(knee=KneeStatus.HISTORY))
        self.assertTrue(history.authorized)
        self.assertEqual(history.max_jog_bout_seconds, 60)
        self.assertEqual(history.walk_jog_ratio, (2, 1))

    def test_hard_surface_with_knee_history_forbids_jogging(self) -> None:
        auth = derive_authorization(profile(
            knee=KneeStatus.HISTORY, surface=Surface.PAVEMENT
        ))
        self.assertNotIn("slow_jog", auth.allowed_modes)
        self.assertIn("R-SURFACE-1", {d.rule_id for d in auth.basis})

    def test_poor_footwear_forbids_jogging(self) -> None:
        auth = derive_authorization(profile(footwear=Footwear.POOR))
        self.assertNotIn("slow_jog", auth.allowed_modes)

    def test_rehab_window_without_doctor_requires_review(self) -> None:
        auth = derive_authorization(profile(
            rehab=RehabStatus.RECOVERING, rehab_weeks_since=3
        ))
        self.assertFalse(auth.authorized)
        self.assertTrue(any("医生" in r or "复核" in r for r in auth.review_reasons))
        rule_ids = {d.rule_id for d in auth.basis}
        self.assertIn("R-REHAB-1", rule_ids)
        self.assertIn("R-REHAB-2", rule_ids)

    def test_rehab_window_with_doctor_approval_is_authorized(self) -> None:
        auth = derive_authorization(profile(
            rehab=RehabStatus.RECOVERING, rehab_weeks_since=3,
            doctor_verdict=MedicalVerdict.APPROVED,
        ))
        self.assertTrue(auth.authorized)
        self.assertEqual(auth.max_session_minutes, 15)

    def test_low_walk_capacity_forbids_jogging(self) -> None:
        auth = derive_authorization(profile(walk_capacity_minutes=5))
        self.assertNotIn("slow_jog", auth.allowed_modes)

    def test_rules_never_loosen(self) -> None:
        # 医生给一个更宽松的心率不应覆盖系统保守值
        auth = derive_authorization(profile(
            doctor_verdict=MedicalVerdict.RESTRICTED,
            doctor_overrides={"max_heart_rate": 200},
        ))
        self.assertEqual(auth.max_heart_rate, 91)


if __name__ == "__main__":
    unittest.main()
