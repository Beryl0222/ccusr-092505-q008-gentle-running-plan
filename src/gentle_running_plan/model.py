"""超慢跑个体处方引擎的核心领域模型。

所有输入（健康筛查、医生复核、目标、动作能力、鞋具场地、训练记录、异常报告）
都是不可变事实；计划以版本演进，每次调整都标注依据未来哪一节课生效。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum


class RiskLevel(str, Enum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


class PlanStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    SUPERSEDED = "superseded"


# 出现即暂停后续训练的危险症状
DANGER_SYMPTOMS = frozenset(
    {"chest_pain", "severe_dizziness", "palpitations", "breathlessness", "fainting"}
)


@dataclass(frozen=True)
class HealthScreening:
    participant_id: str
    recorded_at: datetime
    conditions: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class MedicalClearance:
    """医生/专业复核意见：既是授权负荷上限，也是暂停后恢复训练的唯一凭证。"""

    clearance_id: str
    participant_id: str
    reviewer: str
    recorded_at: datetime
    max_heart_rate: int | None = None
    max_session_minutes: int = 60
    max_weekly_sessions: int = 7
    restrictions: tuple[str, ...] = ()
    resumes_training: bool = True


@dataclass(frozen=True)
class Goal:
    participant_id: str
    kind: str
    target_sessions_per_week: int = 3


@dataclass(frozen=True)
class MovementAssessment:
    participant_id: str
    recorded_at: datetime
    squat_pain: bool = False
    balance_score: int = 2  # 0-3，越低越差
    comfortable_walk_minutes: int = 30


@dataclass(frozen=True)
class EquipmentContext:
    participant_id: str
    recorded_at: datetime
    shoe_cushion: str = "standard"  # minimal | standard | max
    surface: str = "track"  # track | treadmill | concrete | trail


@dataclass(frozen=True)
class IntervalSpec:
    warm_up_minutes: int
    cycles: int
    jog_minutes: int
    walk_minutes: int
    cool_down_minutes: int
    target_hr_max: int | None = None
    target_rpe_max: int | None = None

    @property
    def total_minutes(self) -> int:
        return (
            self.warm_up_minutes
            + self.cycles * (self.jog_minutes + self.walk_minutes)
            + self.cool_down_minutes
        )


@dataclass(frozen=True)
class SessionPrescription:
    index: int  # 1 起始的课次号
    scheduled_on: date
    intervals: IntervalSpec


@dataclass(frozen=True)
class AdjustmentReason:
    trigger: str  # 机器可读的触发码，如 knee_injury / high_rpe
    detail: str  # 面向人的说明
    evidence: tuple[str, ...] = ()  # 支撑证据（记录、复核意见等标识）


@dataclass(frozen=True)
class PlanVersion:
    """计划的一个版本；effective_from_session 之前的课次与上一版完全一致。"""

    participant_id: str
    version: int
    created_at: datetime
    sessions: tuple[SessionPrescription, ...]
    effective_from_session: int
    reasons: tuple[AdjustmentReason, ...] = ()
    status: PlanStatus = PlanStatus.ACTIVE

    def session(self, index: int) -> SessionPrescription | None:
        for item in self.sessions:
            if item.index == index:
                return item
        return None


@dataclass(frozen=True)
class SessionRecord:
    """一次训练上报；原始体征只追加、永不改写。"""

    record_id: str
    participant_id: str
    session_index: int
    device_id: str
    scheduled_slot: datetime  # 去重键：同一学员同一时段只保留一条有效记录
    reported_at: datetime
    avg_heart_rate: int | None = None
    max_heart_rate: int | None = None
    rpe: int | None = None
    completed_minutes: int = 0
    symptoms: tuple[str, ...] = ()


@dataclass(frozen=True)
class SafetyReport:
    report_id: str
    participant_id: str
    reported_at: datetime
    symptoms: tuple[str, ...]
    source: str = "self"  # self | coach | device


@dataclass(frozen=True)
class PauseRecord:
    participant_id: str
    paused_at: datetime
    trigger: str  # danger_symptom | load_exceeds_authorization
    reason: str  # 学员可见的暂停原因
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConflictCase:
    """同一时段数值冲突：保留先到的有效记录，冲突方转入复核，绝不取平均。"""

    participant_id: str
    scheduled_slot: datetime
    kept_record_id: str
    conflicting_record_id: str
    differing_fields: tuple[str, ...]
