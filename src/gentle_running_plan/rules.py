"""健康筛查 -> 授权负荷的规则引擎。

每条阈值都有稳定的规则编号与中文说明，最终授权决定携带 ``basis``，
使日后"为什么这节课只能走跑交替 60 秒"可以逐门槛复核。多条规则并存时
取更严格者；医生意见的优先级最高，同样留痕。

本模块是可审查的保守默认规则集，数值需由站点专业负责人按指南校准。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


# -- 阈值（集中登记，解释链直接引用）----------------------------------------

HR_MAX_FORMULA = "220 - 年龄"
NOVICE_HR_FRACTION = 0.60          # 新手/中老年：不超过 HRmax 60%
HYPERTENSION_HR_FRACTION = 0.55    # 控制良好的高血压再收紧
DEFAULT_MAX_RPE = 3                # Borg CR-10：可完整说话的强度
BASE_SESSION_MINUTES = 20
REHAB_SESSION_MINUTES = 15
REHAB_WINDOW_WEEKS = 12
HISTORY_KNEE_JOG_BOUT_SECONDS = 60
HISTORY_KNEE_WALK_JOG_RATIO = (2, 1)
BP_BLOCK_SBP = 160                 # 静息收缩压 >= 160 暂缓
BP_BLOCK_DBP = 100                 # 静息舒张压 >= 100 暂缓
SENIOR_AGE = 65
SUPERVISED_SESSIONS_NOVICE = 4
MIN_WALK_CAPACITY_MINUTES = 10     # 连续步行不足则先只步行

GENERAL_RED_FLAGS = (
    "胸痛、胸闷或放射至手臂/下颌的不适",
    "明显心悸",
    "与运动强度不符的呼吸困难",
    "眩晕、黑蒙或冷汗",
    "运动后异常持续的疲乏",
)
BP_RED_FLAGS = (
    "搏动性头痛或视物模糊",
    "自测血压达到医生设定的上限",
)
KNEE_RED_FLAGS = (
    "膝关节锐痛、卡顿或打软腿",
    "训练后关节肿胀或次日疼痛加重",
)


class KneeStatus(StrEnum):
    NONE = "none"          # 无膝伤史
    HISTORY = "history"    # 既往膝伤，目前无急性症状
    ACTIVE = "active"      # 近期/当前膝伤未结案


class RehabStatus(StrEnum):
    NONE = "none"
    RECOVERING = "recovering"  # 刚完成康复，仍在重返窗口


class MedicalVerdict(StrEnum):
    ABSENT = "absent"        # 尚无医生意见
    APPROVED = "approved"    # 无条件同意
    RESTRICTED = "restricted"  # 附限制同意
    REJECTED = "rejected"    # 医生认为当前不宜运动


class Footwear(StrEnum):
    GOOD = "good"    # 合脚、支撑足够的慢跑鞋
    FAIR = "fair"    # 可用但已老旧或支撑一般
    POOR = "poor"    # 平底鞋/拖鞋/明显磨损，不适合跑步


class Surface(StrEnum):
    TRACK = "track"          # 塑胶跑道/平整土路
    TREADMILL = "treadmill"  # 跑步机
    PAVEMENT = "pavement"    # 水泥/石板硬地


@dataclass(frozen=True)
class HealthProfile:
    participant_id: str
    age: int
    knee: KneeStatus = KneeStatus.NONE
    resting_sbp: int | None = None
    resting_dbp: int | None = None
    hypertension_controlled: bool = False
    rehab: RehabStatus = RehabStatus.NONE
    rehab_weeks_since: int | None = None
    doctor_verdict: MedicalVerdict = MedicalVerdict.ABSENT
    doctor_overrides: Mapping[str, Any] = field(default_factory=dict)
    doctor_note: str = ""
    footwear: Footwear = Footwear.GOOD
    surface: Surface = Surface.TRACK
    beginner: bool = True
    walk_capacity_minutes: int = 30
    goals: tuple[str, ...] = ()

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> HealthProfile:
        def get(key: str, default: Any) -> Any:
            return data[key] if key in data else default

        goals = get("goals", ())
        return HealthProfile(
            participant_id=str(data["participant_id"]),
            age=int(data["age"]),
            knee=KneeStatus(get("knee", KneeStatus.NONE)),
            resting_sbp=get("resting_sbp", None),
            resting_dbp=get("resting_dbp", None),
            hypertension_controlled=bool(get("hypertension_controlled", False)),
            rehab=RehabStatus(get("rehab", RehabStatus.NONE)),
            rehab_weeks_since=get("rehab_weeks_since", None),
            doctor_verdict=MedicalVerdict(
                get("doctor_verdict", MedicalVerdict.ABSENT)
            ),
            doctor_overrides=dict(get("doctor_overrides", {})),
            doctor_note=get("doctor_note", ""),
            footwear=Footwear(get("footwear", Footwear.GOOD)),
            surface=Surface(get("surface", Surface.TRACK)),
            beginner=bool(get("beginner", True)),
            walk_capacity_minutes=int(get("walk_capacity_minutes", 30)),
            goals=tuple(goals) if goals else (),
        )


@dataclass(frozen=True)
class RuleDecision:
    rule_id: str
    title: str
    effect: str
    severity: str  # block / tighten / supervise / info


@dataclass(frozen=True)
class RiskAuthorization:
    participant_id: str
    authorized: bool
    requires_professional_review: bool
    review_reasons: tuple[str, ...]
    max_heart_rate: int | None
    max_rpe: int | None
    max_session_minutes: int | None
    max_jog_bout_seconds: int | None  # None=不据此限制；0=禁止慢跑段
    walk_jog_ratio: tuple[int, int] | None
    allowed_modes: tuple[str, ...]
    supervision_required: bool
    supervised_sessions: int
    red_flags: tuple[str, ...]
    basis: tuple[RuleDecision, ...]
    doctor_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "participant_id": self.participant_id,
            "authorized": self.authorized,
            "requires_professional_review": self.requires_professional_review,
            "review_reasons": list(self.review_reasons),
            "max_heart_rate": self.max_heart_rate,
            "max_rpe": self.max_rpe,
            "max_session_minutes": self.max_session_minutes,
            "max_jog_bout_seconds": self.max_jog_bout_seconds,
            "walk_jog_ratio": list(self.walk_jog_ratio)
            if self.walk_jog_ratio else None,
            "allowed_modes": list(self.allowed_modes),
            "supervision_required": self.supervision_required,
            "supervised_sessions": self.supervised_sessions,
            "red_flags": list(self.red_flags),
            "basis": [
                {
                    "rule_id": d.rule_id,
                    "title": d.title,
                    "effect": d.effect,
                    "severity": d.severity,
                }
                for d in self.basis
            ],
            "doctor_note": self.doctor_note,
        }


class _Draft:
    """规则累积器：所有上限只允许收紧（取更小），限制只允许增加。"""

    def __init__(self, profile: HealthProfile) -> None:
        self.profile = profile
        self.blocks: list[str] = []
        self.review_reasons: list[str] = []
        self.decisions: list[RuleDecision] = []
        hr_max = 220 - profile.age
        self.max_heart_rate: int | None = round(hr_max * NOVICE_HR_FRACTION)
        self.max_rpe: int | None = DEFAULT_MAX_RPE
        self.max_session_minutes: int | None = BASE_SESSION_MINUTES
        self.max_jog_bout_seconds: int | None = None  # 超慢跑段本身不切分
        self.walk_jog_ratio: tuple[int, int] | None = None
        self.allowed_modes: set[str] = {"walk", "slow_jog"}
        self.supervision_required = False
        self.supervised_sessions = 0
        self.red_flags: list[str] = list(GENERAL_RED_FLAGS)

    def tighten(self, field_name: str, value: Any) -> None:
        current = getattr(self, field_name)
        if current is None:
            setattr(self, field_name, value)
        elif isinstance(value, int) and value < current:
            setattr(self, field_name, value)

    def no_jogging(self, rule_id: str, title: str, effect: str,
                   severity: str = "tighten") -> None:
        self.allowed_modes.discard("slow_jog")
        self.max_jog_bout_seconds = 0
        self.walk_jog_ratio = None
        self.decisions.append(RuleDecision(rule_id, title, effect, severity))

    def decide(self, rule_id: str, title: str, effect: str,
               severity: str = "tighten") -> None:
        self.decisions.append(RuleDecision(rule_id, title, effect, severity))

    def build(self) -> RiskAuthorization:
        blocked = bool(self.blocks)
        authorized = not blocked
        review_reasons = tuple(self.review_reasons) + tuple(
            b for b in self.blocks if b not in self.review_reasons
        )
        requires_review = bool(review_reasons)
        if blocked:
            # 阻断态不输出可被误读为"可以照做"的运动参数
            max_hr = max_rpe = max_minutes = max_bout = None
            ratio = None
            modes: tuple[str, ...] = ()
        else:
            max_hr = self.max_heart_rate
            max_rpe = self.max_rpe
            max_minutes = self.max_session_minutes
            max_bout = self.max_jog_bout_seconds
            ratio = self.walk_jog_ratio
            modes = tuple(m for m in ("walk", "slow_jog")
                          if m in self.allowed_modes)
        return RiskAuthorization(
            participant_id=self.profile.participant_id,
            authorized=authorized,
            requires_professional_review=requires_review,
            review_reasons=review_reasons,
            max_heart_rate=max_hr,
            max_rpe=max_rpe,
            max_session_minutes=max_minutes,
            max_jog_bout_seconds=max_bout,
            walk_jog_ratio=ratio,
            allowed_modes=modes,
            supervision_required=self.supervision_required and authorized,
            supervised_sessions=self.supervised_sessions if authorized else 0,
            red_flags=tuple(self.red_flags),
            basis=tuple(self.decisions),
            doctor_note=self.profile.doctor_note,
        )


def derive_authorization(profile: HealthProfile) -> RiskAuthorization:
    """根据筛查画像推导授权负荷；纯函数，便于逐条测试。"""
    draft = _Draft(profile)
    p = profile

    # R-DOCTOR-1 医生明确拒绝：最高优先级硬禁忌
    if p.doctor_verdict == MedicalVerdict.REJECTED:
        draft.blocks.append("医生意见为当前不宜运动")
        draft.decide(
            "R-DOCTOR-1", "医生拒绝准入",
            "医生明确当前不宜开始运动，禁止安排训练，待新的专业复核",
            "block",
        )

    # R-DOCTOR-2 医生附条件同意：结构化限制与系统规则取更严
    if p.doctor_verdict == MedicalVerdict.RESTRICTED:
        overrides = p.doctor_overrides or {}
        if overrides.get("no_jogging"):
            draft.no_jogging(
                "R-DOCTOR-2", "医生限制慢跑",
                "医生要求现阶段仅步行，不得安排慢跑段",
            )
        if isinstance(overrides.get("max_heart_rate"), int):
            draft.tighten("max_heart_rate", int(overrides["max_heart_rate"]))
        if isinstance(overrides.get("max_rpe"), int):
            draft.tighten("max_rpe", int(overrides["max_rpe"]))
        if isinstance(overrides.get("max_session_minutes"), int):
            draft.tighten("max_session_minutes",
                          int(overrides["max_session_minutes"]))
        if overrides.get("supervised"):
            draft.supervision_required = True
            draft.supervised_sessions = max(draft.supervised_sessions,
                                            SUPERVISED_SESSIONS_NOVICE)
        draft.decide(
            "R-DOCTOR-2", "医生附条件同意",
            "医生限制已与系统保守规则逐项取更严值，原文意见随授权留档",
            "info",
        )

    # R-BP-1 静息血压达到暂缓门槛
    high_sbp = p.resting_sbp is not None and p.resting_sbp >= BP_BLOCK_SBP
    high_dbp = p.resting_dbp is not None and p.resting_dbp >= BP_BLOCK_DBP
    if high_sbp or high_dbp:
        draft.blocks.append(
            f"静息血压达到暂缓门槛（≥{BP_BLOCK_SBP}/{BP_BLOCK_DBP} mmHg）"
        )
        draft.decide(
            "R-BP-1", "血压暂缓门槛",
            f"静息血压 ≥{BP_BLOCK_SBP}/{BP_BLOCK_DBP} mmHg 时不得开始训练，"
            "需医生复核后再恢复",
            "block",
        )

    # R-BP-2 控制良好的高血压：降强度、加红旗症状
    if p.hypertension_controlled:
        hr_max = 220 - p.age
        draft.tighten("max_heart_rate",
                      round(hr_max * HYPERTENSION_HR_FRACTION))
        draft.red_flags.extend(BP_RED_FLAGS)
        draft.decide(
            "R-BP-2", "高血压控制中",
            f"心率上限收紧到 HRmax 的 {int(HYPERTENSION_HR_FRACTION * 100)}%"
            f"（{HR_MAX_FORMULA}），RPE≤{DEFAULT_MAX_RPE}，"
            "避免憋气与寒冷清晨，并监测血压相关红旗症状",
        )

    # R-KNEE-1 活动性膝伤未结案：阻断自动准入
    if p.knee == KneeStatus.ACTIVE:
        draft.blocks.append("存在未结案的活动性膝伤")
        draft.red_flags.extend(KNEE_RED_FLAGS)
        draft.decide(
            "R-KNEE-1", "活动性膝伤",
            "膝伤未结案前不安排跑步训练，需物理治疗/医生复核",
            "block",
        )
    # R-KNEE-2 既往膝伤：走跑交替、单段≤60 秒、注意红旗
    elif p.knee == KneeStatus.HISTORY:
        draft.max_jog_bout_seconds = HISTORY_KNEE_JOG_BOUT_SECONDS
        if draft.walk_jog_ratio is None:
            draft.walk_jog_ratio = HISTORY_KNEE_WALK_JOG_RATIO
        draft.red_flags.extend(KNEE_RED_FLAGS)
        draft.decide(
            "R-KNEE-2", "既往膝伤",
            f"慢跑段不超过 {HISTORY_KNEE_JOG_BOUT_SECONDS} 秒，"
            "走:跑不少于 "
            f"{HISTORY_KNEE_WALK_JOG_RATIO[0]}:{HISTORY_KNEE_WALK_JOG_RATIO[1]}",
        )

    # R-REHAB-1/2 刚完成康复：窗口内缩短时长、需要医生意见
    in_rehab_window = (
        p.rehab == RehabStatus.RECOVERING
        and (p.rehab_weeks_since is None
             or p.rehab_weeks_since < REHAB_WINDOW_WEEKS)
    )
    if in_rehab_window:
        draft.tighten("max_session_minutes", REHAB_SESSION_MINUTES)
        draft.decide(
            "R-REHAB-1", "重返运动窗口",
            f"完成康复 {REHAB_WINDOW_WEEKS} 周内单次训练不超过 "
            f"{REHAB_SESSION_MINUTES} 分钟",
        )
        if p.doctor_verdict == MedicalVerdict.ABSENT:
            draft.blocks.append("处于康复重返窗口且缺少医生意见")
            draft.review_reasons.append(
                "处于康复重返窗口且缺少医生意见，需专业复核后才能开训"
            )
            draft.decide(
                "R-REHAB-2", "康复窗口缺少医生意见",
                "康复后 12 周内未取得医生意见，须先完成专业复核",
                "block",
            )

    # R-GEAR-1 鞋具不具备跑步条件：先步行
    if p.footwear == Footwear.POOR:
        draft.no_jogging(
            "R-GEAR-1", "鞋具不适合跑步",
            "鞋具支撑不足时仅安排步行，更换合适慢跑鞋后复核",
            "info",
        )

    # R-SURFACE-1 硬地叠加膝伤史：取消慢跑段
    if p.surface == Surface.PAVEMENT and p.knee == KneeStatus.HISTORY:
        draft.no_jogging(
            "R-SURFACE-1", "硬地与膝伤史叠加",
            "水泥/石板硬地对既往膝伤冲击过大，改用塑胶场地或跑步机前不慢跑",
        )

    # R-MOBILITY-1 连续步行能力不足：从纯步行起步
    if p.walk_capacity_minutes < MIN_WALK_CAPACITY_MINUTES:
        draft.no_jogging(
            "R-MOBILITY-1", "动作能力不足",
            f"连续步行不足 {MIN_WALK_CAPACITY_MINUTES} 分钟，先建立步行耐力",
            "info",
        )

    # R-SUPERVISE-1 新手或 65 岁以上：前几课需现场指导
    if p.beginner or p.age >= SENIOR_AGE:
        draft.supervision_required = True
        draft.supervised_sessions = max(
            draft.supervised_sessions, SUPERVISED_SESSIONS_NOVICE
        )
        draft.decide(
            "R-SUPERVISE-1", "新手/中老年监督",
            f"新手或 {SENIOR_AGE} 岁及以上学员前 "
            f"{SUPERVISED_SESSIONS_NOVICE} 课需教练现场指导",
            "supervise",
        )

    # R-BASE 基础新手授权（始终登记，便于解释默认值来源）
    draft.decide(
        "R-BASE", "新手超慢跑基线",
        f"按 {HR_MAX_FORMULA} 估算 HRmax，心率上限取 "
        f"{int(NOVICE_HR_FRACTION * 100)}%（约 "
        f"{round((220 - p.age) * NOVICE_HR_FRACTION)} 次/分），"
        f"RPE≤{DEFAULT_MAX_RPE}（可完整说话），单次≤{BASE_SESSION_MINUTES} 分钟，"
        "只允许步行与可说话配速的超慢跑",
        "info",
    )

    return draft.build()
