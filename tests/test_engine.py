from __future__ import annotations

import json
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gentle_running_plan import batch, planning
from gentle_running_plan.contracts import validate_event
from gentle_running_plan.engine import AccessDenied, PrescriptionEngine
from gentle_running_plan.model import (
    EquipmentContext,
    Goal,
    HealthScreening,
    MedicalClearance,
    MovementAssessment,
    PlanStatus,
    SafetyReport,
    SessionRecord,
)
from gentle_running_plan.reminders import ManualClock, Reminder, ReminderScheduler, ReminderStore

TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 10, 1, 8, 0, tzinfo=TZ)
START = date(2026, 10, 5)


def make_engine() -> tuple[PrescriptionEngine, ManualClock]:
    clock = ManualClock(T0)
    return PrescriptionEngine(clock), clock


def register_participant(
    engine: PrescriptionEngine,
    pid: str,
    conditions: tuple[str, ...] = (),
    max_hr: int = 150,
    max_minutes: int = 45,
    max_weekly: int = 5,
) -> None:
    engine.register_screening(HealthScreening(pid, T0, conditions))
    engine.record_clearance(
        MedicalClearance(
            clearance_id=f"clr-{pid}-1",
            participant_id=pid,
            reviewer="dr-wang",
            recorded_at=T0,
            max_heart_rate=max_hr,
            max_session_minutes=max_minutes,
            max_weekly_sessions=max_weekly,
            restrictions=("避免冲刺",) if conditions else (),
        )
    )
    engine.set_goal(Goal(pid, "health", target_sessions_per_week=3))


def make_record(
    pid: str,
    index: int,
    record_id: str | None = None,
    device: str = "watch-1",
    slot: datetime | None = None,
    **values,
) -> SessionRecord:
    defaults = dict(avg_heart_rate=120, max_heart_rate=135, rpe=5, completed_minutes=30)
    defaults.update(values)
    return SessionRecord(
        record_id=record_id or f"rec-{pid}-{index}",
        participant_id=pid,
        session_index=index,
        device_id=device,
        scheduled_slot=slot or datetime(2026, 10, 5, 7, 0, tzinfo=TZ) + timedelta(days=index),
        reported_at=T0,
        **defaults,
    )


class PlanGenerationTests(unittest.TestCase):
    def test_plan_reflects_screening_clearance_and_equipment(self) -> None:
        engine, _ = make_engine()
        register_participant(engine, "p1", conditions=("knee_injury",), max_hr=140, max_minutes=30)
        engine.record_movement(MovementAssessment("p1", T0, squat_pain=True))
        engine.record_equipment(EquipmentContext("p1", T0, shoe_cushion="minimal", surface="concrete"))
        plan = engine.generate_plan("p1", START)

        spec = plan.sessions[0].intervals
        # 膝伤 → 中风险基础（慢跑 2 分钟），下蹲疼痛与硬地薄底各再减 1，下限为 1
        self.assertEqual(1, spec.jog_minutes)
        self.assertEqual(140, spec.target_hr_max)
        self.assertLessEqual(spec.total_minutes, 30)
        self.assertEqual(1, plan.effective_from_session)
        triggers = {r.trigger for r in plan.reasons}
        self.assertIn("risk_level", triggers)
        self.assertIn("squat_pain", triggers)
        self.assertIn("hard_surface_minimal_shoes", triggers)
        self.assertIn("session_minutes_cap", triggers)

    def test_weekly_sessions_capped_by_clearance(self) -> None:
        engine, _ = make_engine()
        register_participant(engine, "p1", max_weekly=2)
        plan = engine.generate_plan("p1", START, weeks=2)
        self.assertEqual(4, len(plan.sessions))  # 每周 2 次 × 2 周
        self.assertIn("weekly_sessions_cap", {r.trigger for r in plan.reasons})


class AdjustmentTests(unittest.TestCase):
    def test_adjustment_takes_effect_from_future_session_and_keeps_history(self) -> None:
        engine, _ = make_engine()
        register_participant(engine, "p1")
        plan_v1 = engine.generate_plan("p1", START)
        engine.ingest_record(make_record("p1", 1, rpe=6))
        engine.ingest_record(make_record("p1", 2, rpe=9))

        plan_v2 = engine.active_plan("p1")
        self.assertEqual(2, plan_v2.version)
        self.assertEqual(3, plan_v2.effective_from_session)
        # 生效课次之前的处方与上一版完全一致
        for index in (1, 2):
            self.assertEqual(plan_v1.session(index), plan_v2.session(index))
        self.assertLess(
            plan_v2.session(3).intervals.cycles, plan_v1.session(3).intervals.cycles
        )
        reason = plan_v2.reasons[0]
        self.assertEqual("reduce_load", reason.trigger)
        self.assertIn("rec-p1-2", reason.evidence)
        # 已完成的训练记录不因计划改写而消失
        self.assertEqual({1, 2}, engine.store.completed_session_indices("p1"))
        self.assertEqual(PlanStatus.SUPERSEDED, engine._plans["p1"][0].status)

    def test_easy_session_progresses_within_clearance_cap(self) -> None:
        engine, _ = make_engine()
        register_participant(engine, "p1", max_hr=150, max_minutes=45)
        engine.generate_plan("p1", START)
        engine.ingest_record(make_record("p1", 1, rpe=2, max_heart_rate=110))
        plan = engine.active_plan("p1")
        self.assertEqual(2, plan.version)
        self.assertEqual("progress_load", plan.reasons[0].trigger)
        self.assertLessEqual(plan.session(2).intervals.total_minutes, 45)


class SafetyTests(unittest.TestCase):
    def test_danger_symptom_pauses_and_only_new_review_resumes(self) -> None:
        engine, clock = make_engine()
        register_participant(engine, "p1")
        engine.generate_plan("p1", START)
        engine.ingest_record(make_record("p1", 1, symptoms=("chest_pain",)))

        view = engine.participant_view("p1")
        self.assertEqual("paused", view.plan_status)
        self.assertIn("危险症状", view.pause_reason)
        self.assertIsNone(view.next_session_index)
        self.assertEqual(PlanStatus.PAUSED, engine.active_plan("p1").status)

        # 暂停期间不再产生新的调整版本
        engine.ingest_record(make_record("p1", 2, rpe=9))
        self.assertEqual(1, engine.active_plan("p1").version)
        # 暂停之前的旧复核意见不能恢复
        stale = MedicalClearance("clr-old", "p1", "dr-wang", T0 - timedelta(days=1))
        engine.record_clearance(stale)
        self.assertEqual("paused", engine.participant_view("p1").plan_status)
        # 暂停之后的新专业复核才能恢复
        clock.advance(days=3)
        engine.record_clearance(
            MedicalClearance("clr-p1-2", "p1", "dr-li", clock(), max_heart_rate=130)
        )
        view = engine.participant_view("p1")
        self.assertEqual("active", view.plan_status)
        self.assertIsNone(view.pause_reason)
        # 第 1、2 课已有记录（暂停期间到达的记录仍按事实保留），从第 3 课继续
        self.assertEqual(3, view.next_session_index)
        plan = engine.active_plan("p1")
        self.assertEqual(3, plan.effective_from_session)
        self.assertEqual("professional_review", plan.reasons[0].trigger)

    def test_load_beyond_authorization_pauses_immediately(self) -> None:
        engine, _ = make_engine()
        register_participant(engine, "p1", max_hr=140)
        engine.generate_plan("p1", START)
        engine.ingest_record(make_record("p1", 1, max_heart_rate=155))
        pause = engine.explain("p1").pauses[0]
        self.assertEqual("load_exceeds_authorization", pause.trigger)
        self.assertEqual("paused", engine.participant_view("p1").plan_status)

    def test_safety_report_with_danger_symptom_pauses(self) -> None:
        engine, _ = make_engine()
        register_participant(engine, "p1")
        engine.generate_plan("p1", START)
        pause = engine.report_safety(
            SafetyReport("sr-1", "p1", T0, ("severe_dizziness",), source="coach")
        )
        self.assertIsNotNone(pause)
        self.assertEqual("paused", engine.participant_view("p1").plan_status)
        # 非危险症状不触发暂停
        engine2, _ = make_engine()
        register_participant(engine2, "p2")
        engine2.generate_plan("p2", START)
        self.assertIsNone(
            engine2.report_safety(SafetyReport("sr-2", "p2", T0, ("muscle_soreness",)))
        )
        self.assertEqual("active", engine2.participant_view("p2").plan_status)


class RecordStoreTests(unittest.TestCase):
    def test_offline_retry_and_duplicate_slot_keep_single_effective_record(self) -> None:
        engine, _ = make_engine()
        register_participant(engine, "p1")
        engine.generate_plan("p1", START)
        record = make_record("p1", 1)
        self.assertEqual("stored", engine.ingest_record(record).outcome)
        # 离线设备重试：同一 record_id 幂等忽略
        self.assertEqual("duplicate", engine.ingest_record(record).outcome)
        # 同一时段同一数值的重复上报也只保留一条有效记录
        twin = make_record("p1", 1, record_id="rec-p1-1b", device="phone-1")
        self.assertEqual("duplicate", engine.ingest_record(twin).outcome)
        self.assertEqual(1, len(engine.store.effective_records("p1")))
        self.assertEqual(2, len(engine.store.all_records("p1")))  # 原始数据都保留

    def test_conflicting_values_go_to_review_not_average(self) -> None:
        engine, _ = make_engine()
        register_participant(engine, "p1")
        engine.generate_plan("p1", START)
        engine.ingest_record(make_record("p1", 1, max_heart_rate=130))
        late = make_record("p1", 1, record_id="rec-late", device="watch-2", max_heart_rate=160)
        result = engine.ingest_record(late)

        self.assertEqual("conflict", result.outcome)
        self.assertEqual(("max_heart_rate",), result.conflict.differing_fields)
        effective = engine.store.effective_records("p1")
        self.assertEqual(1, len(effective))
        # 保留先到记录，绝不取平均（不是 145）
        self.assertEqual(130, effective[0].max_heart_rate)
        # 冲突双方原始体征都不丢失
        self.assertEqual(
            {130, 160}, {r.max_heart_rate for r in engine.store.all_records("p1")}
        )
        self.assertEqual(1, len(engine.store.conflicts("p1")))


class AccessTests(unittest.TestCase):
    def test_coach_sees_only_assigned_participants_and_minimal_info(self) -> None:
        engine, _ = make_engine()
        register_participant(engine, "p1", conditions=("hypertension",))
        engine.generate_plan("p1", START)
        engine.assign_coach("coach-a", "p1")

        view = engine.coach_view("coach-a", "p1")
        self.assertEqual("moderate", view.risk_level)
        self.assertEqual(("避免冲刺",), view.restrictions)
        self.assertEqual(1, view.next_session_index)
        # 最小信息：不包含诊断明细与原始体征字段
        self.assertFalse(hasattr(view, "conditions"))
        self.assertFalse(hasattr(view, "heart_rate"))
        self.assertFalse(hasattr(view, "records"))

        with self.assertRaises(AccessDenied):
            engine.coach_view("coach-b", "p1")
        with self.assertRaises(AccessDenied):
            engine.coach_view("coach-a", "p2")


class BatchTests(unittest.TestCase):
    def test_failed_segments_can_be_retried(self) -> None:
        engine, _ = make_engine()
        register_participant(engine, "ok-1")
        register_participant(engine, "ok-2")
        engine.register_screening(HealthScreening("missing-clearance", T0, ()))  # 缺医生复核

        result = batch.generate_batch(engine, ["ok-1", "missing-clearance", "ok-2"], START)
        self.assertEqual({"ok-1", "ok-2"}, set(result.generated))
        self.assertEqual(["missing-clearance"], list(result.failures))
        self.assertFalse(result.ok)

        # 补齐前置条件后只重试失败片段
        engine.record_clearance(
            MedicalClearance("clr-mc", "missing-clearance", "dr-wang", T0, max_heart_rate=150)
        )
        retried = batch.retry_failed(engine, result, START)
        self.assertTrue(retried.ok)
        self.assertEqual({"ok-1", "ok-2", "missing-clearance"}, set(retried.generated))
        # 已成功的片段未被重复生成
        self.assertIs(result.generated["ok-1"], retried.generated["ok-1"])


class ReminderTests(unittest.TestCase):
    def test_restart_continues_due_reminders_on_injected_calendar(self) -> None:
        clock = ManualClock(T0)
        store = ReminderStore()
        scheduler = ReminderScheduler(clock, store)
        scheduler.schedule(Reminder("r-1", "p1", 3, T0 + timedelta(days=2), "第 3 课训练提醒"))
        scheduler.schedule(Reminder("r-1", "p1", 3, T0 + timedelta(days=2), "重复调度"))  # 幂等
        self.assertEqual([], scheduler.process_due())

        # 服务重启：同一存储 + 同一日历重建调度器
        clock.advance(days=2, minutes=1)
        restarted = ReminderScheduler(clock, store)
        due = restarted.process_due()
        self.assertEqual(["r-1"], [r.reminder_id for r in due])
        # 到期提醒只发送一次
        self.assertEqual([], restarted.process_due())


class ExplainAndEventTests(unittest.TestCase):
    def test_explanation_links_thresholds_versions_and_execution(self) -> None:
        engine, _ = make_engine()
        register_participant(engine, "p1", max_hr=150, max_minutes=45, max_weekly=5)
        engine.generate_plan("p1", START)
        engine.ingest_record(make_record("p1", 1, rpe=9))

        explanation = engine.explain("p1")
        self.assertTrue(any("150" in t for t in explanation.thresholds))
        self.assertTrue(any("45" in t for t in explanation.thresholds))
        self.assertEqual([1, 2], [v.version for v in explanation.versions])
        self.assertEqual(2, explanation.versions[1].effective_from_session)
        self.assertEqual("reduce_load", explanation.versions[1].reasons[0].trigger)
        first = explanation.executions[0]
        self.assertEqual(1, first.session_index)
        self.assertIsNotNone(first.prescribed)
        self.assertEqual(9, first.record.rpe)
        self.assertIsNone(explanation.executions[1].record)

    def test_emitted_events_conform_to_contract(self) -> None:
        schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))
        engine, clock = make_engine()
        register_participant(engine, "p1")
        engine.generate_plan("p1", START)
        engine.ingest_record(make_record("p1", 1, symptoms=("palpitations",)))
        clock.advance(days=1)
        engine.record_clearance(MedicalClearance("clr-p1-2", "p1", "dr-li", clock()))

        types = [e["event_type"] for e in engine.events]
        self.assertIn("SCREENING_APPROVED", types)
        self.assertIn("PLAN_PUBLISHED", types)
        self.assertIn("SESSION_LOGGED", types)
        self.assertIn("SAFETY_PAUSED", types)
        self.assertIn("PLAN_RESUMED", types)
        for event in engine.events:
            self.assertEqual([], validate_event(event, schema), event)


if __name__ == "__main__":
    unittest.main()
