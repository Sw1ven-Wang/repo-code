# 条件经验与后续独立认证

## 解决的问题与边界

旧流程需要一次性准备 DEV、G3、G4b 的外部任务。单个新用户只有当前任务时，无法完成这套认证；不能因此把经验直接发布为全局能力，也不应该让经验随聊天结束而丢失。

现在提供两个独立通道：

1. **项目条件复用**：S1 与真实模型 S2 已通过的经验，可显式绑定本地项目，跨进程、跨聊天复用。仍是建议，不能改变工具权限、可信验收、Memory v1 默认或当前 Release。
2. **前瞻认证队列**：先冻结候选、Host 源码、模型、上下文、Memory profile、环境、预算和任务配额，随后由可信评测接入方逐步提交独立任务。配额凑齐后走原有 S1/S2/S3、G0–G4b 与人工批准流程。

**没有独立证据时，永久发布仍不能保证成功。** 改善的是经验可持续使用、缺口可见、后续可衔接，不是取消独立验收。任务数量只是预先承诺的实验规模，不等于统计显著性或通用能力证明。

## 项目条件经验

复用现有 `SessionExperiences` 的签名授权、适用性筛选、一次注入、应用回执与撤销机制，没有增加第二套检索器。

```python
experiences = app.session_experiences()
project = experiences.create_project(scope, workspace, max_tasks=32)
grant = experiences.admit(project, contract_ref, candidate_id)
run = app.new_task(goal, workspace, session_id=project, task_family=family)
report = await app.execute(run['id'])
```

CLI：`session create-project --scope SCOPE --workspace PATH --max-tasks 32` 返回 session_id；后续 `session admit` 与 `run --session-id ... --task-family ...` 沿用现有接口。

- 保存于同一 Host state-root 的 SQLite；新聊天可继续使用，新的空 state-root 不会自动继承。
- workspace 必须是同一个规范化目录，scope 必须与 Contract 一致；不因目录相邻而自动继承。
- 默认最多绑定 32 个任务，范围 1–128；在绑定时原子计数，失败任务也占额度，重复创建不会重置额度。
- 无会话 TTL，但有任务次数上限、环境绑定、S1/S2 摘要绑定、来源有效性和冲突检查。
- 注入标记为 `PROJECT_CONDITIONAL_ADVISORY`，不能被当作永久 Release。
- 已应用后任务未成功交付会保守撤销；反馈的因果收益始终标为 UNKNOWN，失败不被解释为已证明经验有害。
- 关闭、撤销、次数耗尽都不会因重启或重复 admit 恢复。次数耗尽后继续普通任务不受影响，需人工复核决定新授权方案；当前没有自动续期或重置接口。
- 新授权不可隐式关联任意项目，也不从未通过 S2 的草案产生授权。

修正了空授权会话的提示开销：只有曾经展示过的授权失效时才添加撤销提示，空 A 组不再收到无关的失效提示。

## 前瞻认证接入

Host API：`app.prospective_certification(evaluator)`。评测器仍是现有 `SuiteEvaluator`，工作模型没有这些写接口。

```python
evaluator = SuiteEvaluator(app, vault, provider_factory)
evaluator.runner.isolated_states = True
evaluator.runner.trial_root = external_trial_directory
queue = app.prospective_certification(evaluator)
plan_id = queue.freeze(candidate_id, budget=fixed_budget,
                       counts={'dev': 2, 'promotion': 3, 'final': 2})
# 后续由可信接入方调用 vault.register，注册外部任务并声明来源关系。
# role 为 dev / promotion / final；必须有 v2 适用性、污染分组元数据。
queue.nominate(plan_id, sealed_cohort_id)
status = queue.status(plan_id)
# 只有完整配额、独立 state-root 和所需 Skill qualification 就绪才会执行。
result = await queue.evaluate(plan_id)
```

`freeze` 要求有限的每任务模型调用预算；完整 A/B 上限是两倍任务数乘单任务预算，G4 的既有回归另计。订阅/账户金额计费依旧由 provider 和外部实验驱动负责，不推断费用。

冻结内容包含候选完整结构、基线/目标 Release、Host Python 源码摘要、模型 endpoint 与推理参数、Memory profile、环境、Governor policy、runner 上下文/复核配置、预算与每角色配额。配额每角色 1–16，默认 2/3/2；不同规模不得汇总成同一实验。

同一候选只有一个队列锁；重新调用 freeze 只能返回相同配置。修改候选或 Host 之后需新实验，不混用旧结果。活动基线变化时要求重新评估，不在不同基线上自动继续。

## 状态机

`WAITING_EVIDENCE → READY_FOR_GATES → RUNNING → COMPLETED / REJECTED / INTERRUPTED`

- WAITING_EVIDENCE：status 返回每个角色还缺多少；evaluate 不调用模型，也不阻断普通任务。
- READY_FOR_GATES：不能替换已收集任务。包含结构化 Skill 时，若缺有效 S1/S2/S3 qualification，返回 `next=WAITING_SKILL_QUALIFICATION`。队列不冒签 Skill 验收。
- RUNNING：持有跨进程执行锁，先持久化开始状态，再执行原有评测器。
- COMPLETED：完整 SuiteEvaluator 返回 PASS，**仍未自动发布**。Release.activate 继续要求签名、Skill 验收、G4 完整性、批准和基线 CAS。
- REJECTED：保存原始拒绝结果，不剔除失败任务重新计算。
- INTERRUPTED：异常或进程中断，保留全部尝试；重启不会自动重发可能计费的请求。需人工核查已有门禁/试验记录并决定后续实验，当前不支持从不确定请求点自动恢复。

任务登记和计数使用 SQLite 事务；相同 cohort 重复登记幂等；并发超额登记拒绝。任务指纹或实现内容重复拒绝，不同角色共享污染来源拒绝。相同适用机制可以跨角色，但必须提供不同、可信的来源分组。

任务 metadata 和内容摘要在执行前复核。合并 cohort 时保留真实 applicability_family 和 contamination_groups，不用队列 ID 改名绕过历史污染检查。后续仍由 HoldoutVault 检查历史曝光、祖先污染及一次性占用。

可信来源分组是接入方声明，不能仅凭文件摘要自动证明语义独立；相同代码改名、来源造假、精心挑选容易成功的任务都不是有效独立验证。已用作训练/调试的任务必须登记曝光，不能因“新聊天”改作隐藏集。

## 观察反馈与认证证据

`queue.observe(plan_id, run_id)` 只接受终态的真实本地 Run，保存 manifest 摘要和结果，明确标为 `OBSERVATIONAL_ONLY / counts_for_gates=False`。无论普通任务成功多少次，都不增加认证配额或生成 G3。

配额收集的是**尚未用于这轮结果筛选的封存任务输入**；正式 A/B 在完整集合上执行。不会逐题看成绩、挑好结果凑门槛，也不假装同一历史 trace 重放多次是多个独立样本。

G3 判断仍沿用当前实现：候选质量不退化、有至少一项修复收益或既定成本收益、成本有界且已结算。G4 与 G4b 不变。Skill 自身 S3 的跨适用范围、负探针等要求不变。

## 数据存储和恢复

| 对象 | 内容 | 生命周期 |
|---|---|---|
| experience_session | 项目路径、scope、授权任务额度 | 显式关闭；不自动删除 |
| session_run / session_grant | 签名绑定、S1/S2 摘要和精确 revision | 不可变；单独状态撤销 |
| session_feedback | 实际应用、交付状态、用量 | 保留，非因果证明 |
| certification_plan | 签名冻结配置 | 不可变 |
| certification_state | 已收集 cohort、计数、执行状态与结果 | 事务更新 |
| certification_nomination | cohort 元数据摘要、收集时间 | 不可变 |
| certification_observation | 普通任务观察 | 不可变、不得作门禁 |
| cohort / trial_* / gate | 原评测器的输入、真实执行和签名结论 | 原生命周期不变 |

不分发任何用户数据集；任务由本地项目、后续用户授权任务或外部可信评测服务提供。源码包只含逻辑和协议测试。不会自动下载 benchmark，也不会在用户日常工作区执行双份 A/B；认证 runner 必须用独立 state-root 和任务快照。

## 本轮验收与未证明项

协议测试覆盖持久化、原生路由、撤销、配额、配置变更、重复/污染、普通反馈隔离、异常中断和现有门禁联调。脚本模型只验证流程，不能证明真实模型效果。

本轮不重跑真实模型、不宣称当前 SymPy 草案已获项目授权或永久发布。此前该草案 S2 的交付复核失败不会被本功能自动修复或绕过。无新独立数据时，永久认证仍等待；有经验且局部验收成功时，可以在明确项目范围内发挥作用。

未加入：自动生成可信独立 oracle、自动判断语义近重复、无人值守选题/计费后台服务、跨 Host 搬运签名授权、自动续期、放宽门槛、性能提升宣称。
