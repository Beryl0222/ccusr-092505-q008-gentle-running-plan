# 超慢跑个体处方引擎

本项目提供超慢跑个体处方引擎：接收健康筛查、医生复核意见、目标、动作能力、鞋具与场地条件、训练记录和异常报告，产出带版本的个人训练计划，并对每次调整给出可解释的依据。仓库同时保留底层的领域事件交换约定与基础校验库。

## 目录

- `contracts/domain.schema.json`：领域事件信封和已登记类型。
- `data/sample.json`：中文联调样例。
- `src/gentle_running_plan/contracts.py`：不依赖第三方包的基础校验器。
- `src/gentle_running_plan/model.py`：核心领域模型（筛查、复核、计划版本、间歇、训练记录等）。
- `src/gentle_running_plan/records.py`：只追加训练记录存储。
- `src/gentle_running_plan/safety.py`：危险症状与授权负荷门槛、暂停/恢复判定。
- `src/gentle_running_plan/planning.py`：计划生成与调整规则。
- `src/gentle_running_plan/engine.py`：处方引擎门面、权限视图与可解释输出。
- `src/gentle_running_plan/batch.py`：批量生成计划与失败片段重试。
- `src/gentle_running_plan/reminders.py`：可注入日历的到期提醒调度。
- `tests/`：契约边界检查与引擎行为测试。

## 关键行为约定

- **调整生效课次**：每次计划调整生成新版本，`effective_from_session` 标注依据未来哪一节课生效；生效之前的课次与上一版完全一致。
- **安全暂停**：训练记录或异常报告出现危险症状，或最高心率超出医生复核授权上限时，立即暂停后续训练；只有暂停之后由专业人员出具的新复核意见才能恢复，恢复时从下一节未完成课次重排。
- **记录不可改写**：已完成的训练和原始体征只追加、不修改；离线设备重试或同一时段重复上报只保留一条有效记录，数值冲突转入复核队列，绝不取平均。
- **最小信息可见**：教练只能查看其名下学员的风险等级、限制事项、计划状态与下次课次；学员可查看自己的暂停原因和下次安排。
- **批量与提醒**：批量生成计划时单个学员失败不影响整体，失败片段可单独重试；提醒调度的时钟与存储均可注入，服务重启后沿同一日历继续处理到期提醒，每条提醒只发送一次。
- **可解释性**：`explain()` 输出风险门槛（来自医生复核）、每个计划版本的调整原因与证据、以及每节课的处方与实际执行对照。
- 引擎关键状态变化会追加符合 `contracts/domain.schema.json` 的领域事件（SCREENING_APPROVED、PLAN_PUBLISHED、SESSION_LOGGED、SAFETY_PAUSED、PLAN_RESUMED）。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```
