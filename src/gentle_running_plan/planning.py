"""计划生成与调整规则。

每条规则都把触发原因写成 AdjustmentReason，调整只作用于未来课次：
新版本计划的 effective_from_session 之前的课次与上一版完全一致。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta

from .model import (
    AdjustmentReason,
    EquipmentContext,
    Goal,
    HealthScreening,
    IntervalSpec,
    MedicalClearance,
    MovementAssessment,
    PlanVersion,
    RiskLevel,
    SessionPrescription,
    SessionRecord,
)

_BASE_INTERVALS = {
    RiskLevel.LOW: dict(warm_up_minutes=5, cycles=6, jog_minutes=3, walk_minutes=2, cool_down_minutes=5),
    RiskLevel.MODERATE: dict(warm_up_minutes=8, cycles=5, jog_minutes=2, walk_minutes=3, cool_down_minutes=5),
    RiskLevel.HIGH: dict(warm_up_minutes=10, cycles=4, jog_minutes=1, walk_minutes=3, cool_down_minutes=5),
}

_HIGH_RISK_CONDITIONS = {"post_rehab", "cardiac_history"}
_MODERATE_RISK_CONDITIONS = {"knee_injury", "hypertension", "balance_issue"}


def risk_level_for(screening: HealthScreening) -> RiskLevel:
    conditions = set(screening.conditions)
    if conditions & _HIGH_RISK_CONDITIONS:
        return RiskLevel.HIGH
    if conditions & _MODERATE_RISK_CONDITIONS:
        return RiskLevel.MODERATE
    return RiskLevel.LOW


def _soften(params: dict) -> None:
    """降低强度：优先缩短慢跑段，已到下限则延长步行段。"""
    if params["jog_minutes"] > 1:
        params["jog_minutes"] -= 1
    else:
        params["walk_minutes"] += 1


def build_intervals(
    risk: RiskLevel,
    clearance: MedicalClearance,
    movement: MovementAssessment | None,
    equipment: EquipmentContext | None,
    reasons: list[AdjustmentReason],
) -> IntervalSpec:
    params = dict(_BASE_INTERVALS[risk])
    if movement is not None and movement.squat_pain:
        _soften(params)
        reasons.append(AdjustmentReason("squat_pain", "下蹲疼痛，降低间歇强度"))
    if movement is not None and movement.balance_score <= 1:
        params["walk_minutes"] += 1
        reasons.append(AdjustmentReason("low_balance", "平衡能力较弱，步行段延长 1 分钟"))
    if (
        equipment is not None
        and equipment.surface == "concrete"
        and equipment.shoe_cushion == "minimal"
    ):
        _soften(params)
        reasons.append(
            AdjustmentReason("hard_surface_minimal_shoes", "硬地面且鞋缓冲不足，降低间歇强度")
        )
    spec = IntervalSpec(
        **params,
        target_hr_max=clearance.max_heart_rate,
        target_rpe_max=5 if risk is RiskLevel.HIGH else 7,
    )
    if spec.total_minutes > clearance.max_session_minutes:
        cycles = max(
            1,
            (clearance.max_session_minutes - spec.warm_up_minutes - spec.cool_down_minutes)
            // (spec.jog_minutes + spec.walk_minutes),
        )
        reasons.append(
            AdjustmentReason(
                "session_minutes_cap",
                f"受医生复核单次 {clearance.max_session_minutes} 分钟上限约束，间歇组数减为 {cycles}",
                (clearance.clearance_id,),
            )
        )
        spec = replace(spec, cycles=cycles)
    return spec


def generate_plan(
    participant_id: str,
    version: int,
    start_date: date,
    screening: HealthScreening,
    clearance: MedicalClearance,
    goal: Goal | None,
    movement: MovementAssessment | None,
    equipment: EquipmentContext | None,
    now: datetime,
    weeks: int = 4,
) -> PlanVersion:
    risk = risk_level_for(screening)
    reasons: list[AdjustmentReason] = [
        AdjustmentReason(
            "risk_level",
            f"健康筛查风险等级 {risk.value}"
            + (f"（{'、'.join(screening.conditions)}）" if screening.conditions else ""),
        )
    ]
    spec = build_intervals(risk, clearance, movement, equipment, reasons)
    per_week = goal.target_sessions_per_week if goal is not None else 3
    if per_week > clearance.max_weekly_sessions:
        reasons.append(
            AdjustmentReason(
                "weekly_sessions_cap",
                f"目标每周 {per_week} 次，受医生复核每周 {clearance.max_weekly_sessions} 次上限约束",
                (clearance.clearance_id,),
            )
        )
        per_week = clearance.max_weekly_sessions
    interval_days = max(1, 7 // max(1, per_week))
    sessions = tuple(
        SessionPrescription(
            index=i + 1,
            scheduled_on=start_date + timedelta(days=i * interval_days),
            intervals=spec,
        )
        for i in range(per_week * weeks)
    )
    return PlanVersion(
        participant_id=participant_id,
        version=version,
        created_at=now,
        sessions=sessions,
        effective_from_session=1,
        reasons=tuple(reasons),
    )


def adjust_for_record(
    plan: PlanVersion,
    record: SessionRecord,
    clearance: MedicalClearance | None,
    now: datetime,
) -> PlanVersion | None:
    """根据一节已完成训练的实际感受调整后续课次；不需要调整时返回 None。"""
    prescribed = plan.session(record.session_index)
    if prescribed is None:
        return None
    spec = prescribed.intervals
    near_cap = (
        clearance is not None
        and clearance.max_heart_rate is not None
        and record.max_heart_rate is not None
        and record.max_heart_rate >= clearance.max_heart_rate - 5
    )
    if (record.rpe is not None and record.rpe >= 8) or near_cap:
        why = (
            f"最高心率 {record.max_heart_rate} 接近授权上限 {clearance.max_heart_rate}"
            if near_cap and clearance is not None
            else f"主观感受 RPE={record.rpe} 偏硬"
        )
        new_spec = replace(spec, cycles=max(1, spec.cycles - 1))
        reason = AdjustmentReason(
            "reduce_load",
            f"第 {record.session_index} 课{why}，后续课次减少 1 组间歇",
            (record.record_id,),
        )
    elif (
        record.rpe is not None
        and record.rpe <= 3
        and (
            clearance is None
            or clearance.max_heart_rate is None
            or record.max_heart_rate is None
            or record.max_heart_rate <= clearance.max_heart_rate - 20
        )
    ):
        candidate = replace(spec, cycles=spec.cycles + 1)
        if clearance is not None and candidate.total_minutes > clearance.max_session_minutes:
            return None
        new_spec = candidate
        reason = AdjustmentReason(
            "progress_load",
            f"第 {record.session_index} 课完成轻松（RPE={record.rpe}），后续课次增加 1 组间歇",
            (record.record_id,),
        )
    else:
        return None
    effective_from = record.session_index + 1
    sessions = tuple(
        s if s.index < effective_from else replace(s, intervals=new_spec) for s in plan.sessions
    )
    return PlanVersion(
        participant_id=plan.participant_id,
        version=plan.version + 1,
        created_at=now,
        sessions=sessions,
        effective_from_session=effective_from,
        reasons=(reason,),
    )


def reschedule_remaining(
    plan: PlanVersion,
    from_session: int,
    start_date: date,
    now: datetime,
    reason: AdjustmentReason,
) -> PlanVersion:
    """恢复训练时重排剩余课次日期；已完成的课次原样保留。"""
    gap_days = 2
    if len(plan.sessions) >= 2:
        gap_days = max(
            1, (plan.sessions[1].scheduled_on - plan.sessions[0].scheduled_on).days
        )
    sessions: list[SessionPrescription] = []
    offset = 0
    for s in plan.sessions:
        if s.index < from_session:
            sessions.append(s)
        else:
            sessions.append(replace(s, scheduled_on=start_date + timedelta(days=offset * gap_days)))
            offset += 1
    return PlanVersion(
        participant_id=plan.participant_id,
        version=plan.version + 1,
        created_at=now,
        sessions=tuple(sessions),
        effective_from_session=from_session,
        reasons=(reason,),
    )
