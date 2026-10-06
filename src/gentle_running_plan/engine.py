"""个体处方引擎门面。

接收筛查、复核、目标、动作能力、鞋具场地、训练记录与异常报告，
维护计划版本与安全状态，输出最小化视图和可解释的处方变化说明。
关键状态变化同时以领域事件形式追加到事件日志（符合 contracts 契约）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Callable

from . import planning, safety
from .model import (
    AdjustmentReason,
    EquipmentContext,
    Goal,
    HealthScreening,
    IntervalSpec,
    MedicalClearance,
    MovementAssessment,
    PauseRecord,
    PlanStatus,
    PlanVersion,
    SafetyReport,
    SessionPrescription,
    SessionRecord,
)
from .records import IngestResult, RecordStore

_ONE_DAY = timedelta(days=1)


class AccessDenied(PermissionError):
    """教练试图查看未授权学员时抛出。"""


@dataclass(frozen=True)
class CoachView:
    """教练可见的最小健康信息：不含诊断明细与原始体征。"""

    participant_id: str
    risk_level: str
    plan_status: str
    restrictions: tuple[str, ...]
    next_session_index: int | None
    next_session_date: date | None


@dataclass(frozen=True)
class ParticipantView:
    participant_id: str
    plan_status: str
    pause_reason: str | None
    next_session_index: int | None
    next_session_date: date | None


@dataclass(frozen=True)
class VersionExplanation:
    version: int
    status: str
    effective_from_session: int
    reasons: tuple[AdjustmentReason, ...]


@dataclass(frozen=True)
class SessionExecution:
    session_index: int
    prescribed: IntervalSpec
    record: SessionRecord | None


@dataclass(frozen=True)
class Explanation:
    """处方变化、风险门槛与实际执行之间的完整对照。"""

    participant_id: str
    thresholds: tuple[str, ...]
    versions: tuple[VersionExplanation, ...]
    executions: tuple[SessionExecution, ...]
    pauses: tuple[PauseRecord, ...]


class PrescriptionEngine:
    def __init__(self, clock: Callable[[], datetime], store: RecordStore | None = None) -> None:
        self._now = clock
        self.store = store or RecordStore()
        self._screenings: dict[str, HealthScreening] = {}
        self._clearances: dict[str, MedicalClearance] = {}
        self._goals: dict[str, Goal] = {}
        self._movements: dict[str, MovementAssessment] = {}
        self._equipment: dict[str, EquipmentContext] = {}
        self._plans: dict[str, list[PlanVersion]] = {}
        self._pauses: dict[str, PauseRecord] = {}
        self._pause_history: dict[str, list[PauseRecord]] = {}
        self._coaches: dict[str, set[str]] = {}
        self.events: list[dict] = []
        self._event_seq = 0

    # ---- 输入登记 -------------------------------------------------------

    def register_screening(self, screening: HealthScreening) -> None:
        self._screenings[screening.participant_id] = screening
        self._emit(
            "SCREENING_APPROVED",
            "participant_screening",
            screening.participant_id,
            "健康筛查已登记",
        )

    def record_clearance(self, clearance: MedicalClearance) -> None:
        """登记医生复核意见；若存在有效暂停且复核满足恢复条件，则恢复训练。"""
        pid = clearance.participant_id
        self._clearances[pid] = clearance
        pause = self._pauses.get(pid)
        if pause is not None and safety.can_resume(pause, clearance):
            del self._pauses[pid]
            plan = self.active_plan(pid)
            next_index = self._next_incomplete_index(pid)
            if plan is not None and next_index is not None:
                reason = AdjustmentReason(
                    "professional_review",
                    f"经 {clearance.reviewer} 出具新复核意见，恢复训练并重排剩余课次",
                    (clearance.clearance_id,),
                )
                resumed = planning.reschedule_remaining(
                    plan, next_index, self._now().date() + _ONE_DAY, self._now(), reason
                )
                self._plans[pid].append(resumed)
                self._emit(
                    "PLAN_RESUMED",
                    "exercise_plan",
                    pid,
                    f"经 {clearance.reviewer} 复核恢复训练，自第 {next_index} 课继续",
                    resumed.version,
                )
            else:
                self._emit(
                    "PLAN_RESUMED", "exercise_plan", pid,
                    f"经 {clearance.reviewer} 复核恢复训练",
                )

    def set_goal(self, goal: Goal) -> None:
        self._goals[goal.participant_id] = goal

    def record_movement(self, movement: MovementAssessment) -> None:
        self._movements[movement.participant_id] = movement

    def record_equipment(self, equipment: EquipmentContext) -> None:
        self._equipment[equipment.participant_id] = equipment

    def assign_coach(self, coach_id: str, participant_id: str) -> None:
        self._coaches.setdefault(coach_id, set()).add(participant_id)

    # ---- 计划 -----------------------------------------------------------

    def generate_plan(self, participant_id: str, start_date: date, weeks: int = 4) -> PlanVersion:
        screening = self._screenings.get(participant_id)
        if screening is None:
            raise ValueError(f"{participant_id} 缺少健康筛查，无法生成计划")
        clearance = self._clearances.get(participant_id)
        if clearance is None:
            raise ValueError(f"{participant_id} 缺少医生复核意见，无法生成计划")
        if participant_id in self._pauses:
            raise ValueError(f"{participant_id} 训练已暂停，需专业复核恢复后才能生成计划")
        plan = planning.generate_plan(
            participant_id,
            version=len(self._plans.get(participant_id, [])) + 1,
            start_date=start_date,
            screening=screening,
            clearance=clearance,
            goal=self._goals.get(participant_id),
            movement=self._movements.get(participant_id),
            equipment=self._equipment.get(participant_id),
            now=self._now(),
            weeks=weeks,
        )
        self._plans.setdefault(participant_id, []).append(plan)
        self._emit(
            "PLAN_PUBLISHED",
            "exercise_plan",
            participant_id,
            f"第 {plan.version} 版计划已发布，自第 {plan.effective_from_session} 课生效",
            plan.version,
        )
        return plan

    def active_plan(self, participant_id: str) -> PlanVersion | None:
        versions = self._plans.get(participant_id)
        return versions[-1] if versions else None

    # ---- 训练记录与异常 --------------------------------------------------

    def ingest_record(self, record: SessionRecord) -> IngestResult:
        """接收训练上报；幂等去重，数值冲突转复核，危险信号立即暂停。"""
        pid = record.participant_id
        result = self.store.ingest(record)
        if result.outcome != "stored":
            return result
        self._emit(
            "SESSION_LOGGED",
            "training_session",
            pid,
            f"第 {record.session_index} 课训练记录已登记",
        )
        pause = safety.evaluate_record(record, self._clearances.get(pid), self._now())
        if pause is not None:
            self._pause(pid, pause)
        elif pid not in self._pauses:
            self._maybe_adjust(record)
        return result

    def report_safety(self, report: SafetyReport) -> PauseRecord | None:
        pause = safety.evaluate_report(report, self._now())
        if pause is not None:
            self._pause(report.participant_id, pause)
        return pause

    def _pause(self, participant_id: str, pause: PauseRecord) -> None:
        if participant_id not in self._pauses:
            self._pauses[participant_id] = pause
        self._pause_history.setdefault(participant_id, []).append(pause)
        plan = self.active_plan(participant_id)
        if plan is not None and plan.status is PlanStatus.ACTIVE:
            self._plans[participant_id][-1] = replace(plan, status=PlanStatus.PAUSED)
        self._emit("SAFETY_PAUSED", "safety_report", participant_id, pause.reason)

    def _maybe_adjust(self, record: SessionRecord) -> None:
        pid = record.participant_id
        plan = self.active_plan(pid)
        if plan is None or plan.status is not PlanStatus.ACTIVE:
            return
        adjusted = planning.adjust_for_record(
            plan, record, self._clearances.get(pid), self._now()
        )
        if adjusted is None:
            return
        self._plans[pid][-1] = replace(plan, status=PlanStatus.SUPERSEDED)
        self._plans[pid].append(adjusted)
        self._emit(
            "PLAN_PUBLISHED",
            "exercise_plan",
            pid,
            f"第 {adjusted.version} 版计划已发布，自第 {adjusted.effective_from_session} 课生效",
            adjusted.version,
        )

    # ---- 视图 -----------------------------------------------------------

    def coach_view(self, coach_id: str, participant_id: str) -> CoachView:
        if participant_id not in self._coaches.get(coach_id, set()):
            raise AccessDenied(f"教练 {coach_id} 无权查看学员 {participant_id}")
        plan = self.active_plan(participant_id)
        paused = participant_id in self._pauses
        nxt = None if paused else self._next_session(participant_id)
        clearance = self._clearances.get(participant_id)
        screening = self._screenings.get(participant_id)
        return CoachView(
            participant_id=participant_id,
            risk_level=planning.risk_level_for(screening).value if screening else "unknown",
            plan_status="paused" if paused else (plan.status.value if plan else "none"),
            restrictions=clearance.restrictions if clearance else (),
            next_session_index=nxt.index if nxt else None,
            next_session_date=nxt.scheduled_on if nxt else None,
        )

    def participant_view(self, participant_id: str) -> ParticipantView:
        plan = self.active_plan(participant_id)
        pause = self._pauses.get(participant_id)
        nxt = None if pause is not None else self._next_session(participant_id)
        return ParticipantView(
            participant_id=participant_id,
            plan_status="paused" if pause is not None else (plan.status.value if plan else "none"),
            pause_reason=pause.reason if pause is not None else None,
            next_session_index=nxt.index if nxt else None,
            next_session_date=nxt.scheduled_on if nxt else None,
        )

    def explain(self, participant_id: str) -> Explanation:
        clearance = self._clearances.get(participant_id)
        thresholds: list[str] = []
        if clearance is not None:
            if clearance.max_heart_rate is not None:
                thresholds.append(
                    f"最高心率不超过 {clearance.max_heart_rate} 次/分（{clearance.reviewer} 复核）"
                )
            thresholds.append(f"单次训练不超过 {clearance.max_session_minutes} 分钟")
            thresholds.append(f"每周训练不超过 {clearance.max_weekly_sessions} 次")
        versions = tuple(
            VersionExplanation(v.version, v.status.value, v.effective_from_session, v.reasons)
            for v in self._plans.get(participant_id, [])
        )
        records = {r.session_index: r for r in self.store.effective_records(participant_id)}
        plan = self.active_plan(participant_id)
        executions = tuple(
            SessionExecution(s.index, s.intervals, records.get(s.index))
            for s in (plan.sessions if plan else ())
        )
        return Explanation(
            participant_id=participant_id,
            thresholds=tuple(thresholds),
            versions=versions,
            executions=executions,
            pauses=tuple(self._pause_history.get(participant_id, [])),
        )

    # ---- 内部 -------------------------------------------------------------

    def _next_incomplete_index(self, participant_id: str) -> int | None:
        plan = self.active_plan(participant_id)
        if plan is None:
            return None
        done = self.store.completed_session_indices(participant_id)
        for s in plan.sessions:
            if s.index not in done:
                return s.index
        return None

    def _next_session(self, participant_id: str) -> SessionPrescription | None:
        plan = self.active_plan(participant_id)
        if plan is None or plan.status is not PlanStatus.ACTIVE:
            return None
        index = self._next_incomplete_index(participant_id)
        return plan.session(index) if index is not None else None

    def _emit(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        summary: str,
        version: int = 1,
    ) -> None:
        self._event_seq += 1
        self.events.append(
            {
                "event_id": f"evt-{self._event_seq:06d}",
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "occurred_at": self._now().isoformat(),
                "version": version,
                "summary": summary,
            }
        )
