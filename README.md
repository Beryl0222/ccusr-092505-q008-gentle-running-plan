# 超慢跑个体处方引擎

面向社区运动站新手与中老年人的超慢跑**个体处方引擎**：以仅追加的领域事件
为唯一事实来源，把健康筛查、医生意见、目标、动作能力、鞋具场地、计划版本、
间歇安排、心率/主观感受与异常报告串成可复核的闭环。

## 设计原则

1. **事件不可变**：筛查、训练、暂停、复核都是只追加的事件；计划改写不会删除
   已完成的训练和原始体征（`store.py`，流内版本连续 + 乐观并发 + 全局
   `event_id` 幂等，JSONL 持久化）。
2. **调整只对未来课次生效**：每次发布/调整是一个计划版本，必须声明
   `effective_from_seq`；早于生效点的课次与上一版逐字一致，执行时按课次
   选择适用版本（`plan.py`）。
3. **安全优先且可恢复**：红旗症状或体征超出授权负荷时，`SESSION_LOGGED`
   与 `SAFETY_PAUSED` 同批原子写入，立即暂停后续所有课次；只有显式引用
   本次暂停的**新专业复核**（`REVIEW_CLEARED`）才能 `PLAN_RESUMED`
   （`safety.py`、`screening.py`）。
4. **风险门槛可溯源**：每条阈值有稳定规则编号（R-BASE、R-BP-1/2、
   R-KNEE-1/2、R-REHAB-1/2、R-DOCTOR-1/2、R-GEAR-1、R-SURFACE-1、
   R-MOBILITY-1、R-SUPERVISE-1），授权决定携带 `basis`，计划发布时固化
   授权快照（`rules.py`）。多规则并存取更严者，医生附条件同意同样逐门槛
   取严，医生明确拒绝直接阻断。
5. **上报一次有效**：设备/表格携带 `report_id` 幂等键，重放保持一条记录；
   同一课次（无课次按自然日）重复上报数值一致保留先到记录，**数值冲突写入
   `REPORT_CONFLICTED` 转人工复核，绝不取平均**；离线晚到照常登记
   （`ingest.py`）。
6. **最小必要信息**：教练只能看到本人学员（Roster 绑定）的训练限制与
   安全状态，不暴露血压原值与医生备注原文；学员可查看本人暂停原因、恢复
   条件与下次安排（`views.py`）。
7. **解释链**：`explain.py` 输出"筛查门槛 → 计划版本/生效课次 → 执行
   体征 → 红旗/越限 → 暂停 → 新复核 → 恢复/调整"的时间线，每步引用
   事件 id 与规则编号。
8. **可注入日历**：所有服务依赖 `Clock`（系统/固定/推进时钟）。提醒队列
   完全由事件重建，服务重启后继续投递到期提醒，每条约一次，通道失败不
   标记已发；计划调整只作废受影响课次的旧提醒，暂停期不打扰
   （`reminders.py`、`clock.py`）。
9. **批量片段重试**：批量生成按参与者分片，单片失败不影响其他片，重试只
   重放失败片段（`batch.py`）。

> `rules.py` 中的数值是可审查的保守默认集，上线前应由站点专业负责人按
> 现行运动指南与医嘱校准。

## 目录

- `contracts/domain.schema.json`：领域事件信封与已登记类型。
- `data/sample.json`：中文联调样例。
- `src/gentle_running_plan/`
  - `contracts.py`：不依赖第三方包的交换层校验器。
  - `events.py` / `store.py` / `clock.py` / `ids.py`：事件信封、仅追加
    存储、时间抽象、聚合流标识。
  - `rules.py` / `screening.py`：风险授权规则与筛查/复核服务。
  - `plan.py`：版本化处方与授权负荷校验。
  - `ingest.py` / `safety.py`：上报去重/冲突与安全暂停/恢复。
  - `reminders.py` / `batch.py`：重启续处理提醒与批量重试。
  - `views.py` / `explain.py`：角色视图与解释链。
  - `engine.py`：装配门面。
- `tests/`：契约与领域规则测试。

## 事件与聚合

| 事件 | 聚合流 | 含义 |
| --- | --- | --- |
| `SCREENING_APPROVED` / `SCREENING_SUBMITTED` | `participant:<id>` | 筛查登记并附授权推导（通过/待复核） |
| `REVIEW_RECORDED` / `REVIEW_CLEARED` | `participant:<id>` | 专业复核；`CLEARED` 可显式解除某次暂停 |
| `PLAN_PUBLISHED` / `PLAN_ADJUSTED` | `plan:<id>` | 计划版本，调整含 `effective_from_seq` |
| `SESSION_LOGGED` / `REPORT_CONFLICTED` | `session:<id>:<seq>` | 有效训练记录 / 冲突或暂停期活动转复核 |
| `SAFETY_PAUSED` | `safety:<id>` | 红旗或越限，立即暂停后续课次 |
| `PLAN_RESUMED` | `plan:<id>` | 凭新复核恢复 |
| `REMINDER_REQUESTED/DELIVERED/CANCELLED` | `reminder:<id>` | 提醒排期、投递、作废 |

## 快速示例

```python
from gentle_running_plan import PrescriptionEngine, FixedClock, HealthProfile, SessionSpec, Interval

engine = PrescriptionEngine(clock=FixedClock("2026-10-06T08:00:00+08:00"))
engine.screening.submit(HealthProfile(participant_id="p1", age=68))
engine.plans.publish("p1", [SessionSpec(
    seq=1, scheduled_at="2026-10-07T08:00:00+08:00",
    warmup_minutes=5, cooldown_minutes=5,
    intervals=(Interval("walk", 120, 3), Interval("slow_jog", 60, 3)),
    targets={"max_heart_rate": 91, "max_rpe": 3},
)])
result = engine.ingest.log_report("p1", {
    "report_id": "watch-001", "session_seq": 1,
    "observed_at": "2026-10-07T08:40:00+08:00",
    "readings": {"peak_heart_rate": 142}, "symptoms": [],
})
assert result.paused  # 超授权心率，后续训练立即暂停
```

## 测试

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests
```
