# repo-code v1.5｜历次 Review 修复矩阵与强制反例

> 状态：`DESIGN_REVIEW_V1.5`；均为**架构与协议设计**，并非已实现的软件安全测试结果。  
> 对应：[01｜总体架构](01_repo-code_Architecture_and_Business.md) · [02｜技术与接口契约](02_repo-code_RSI_Technical_Design.md) · [README](README.md)  
> 本轮评审：**2 P1**（另外继承 v1.4 的 **2 P1 + 4 P2**）。P1 = 相关功能上线前不可遗漏；P2 = 工程契约需补齐，按其依赖阶段阻断完成声明。

## 0. 第三轮审查：两项 P1 结论与修复范围

| 项目 | 根因 | 防护位置 | 最晚实现阶段 |
|---|---|---|---|
| **P1-G · 全部必需检查未遍历** | 只检查 `required_checks` 非空却根据覆盖矩阵来实际遍历，导致未映射的静态/安全必需检查被漏掉 | 技术 §4.3.1、§13.1.3；架构 §2.1、§10.1 | **S1 / VERIFIED 前** |
| **P1-H · Steering/Cancel 与验收提交竞态** | 评测完成后异步调用 `record_outcome`，未与事件接纳在同一事务重新校验 Run 状态，导致旧完成结果覆盖新意图 | 技术 §4.1、§4.3.2、§4.5、§13.1.4；架构 §4.1、§5.1 | **S1 最小栅栏、S2 完整 Steering** |

## 0.1 P1-G：冻结 Contract + 提交时双重验证

| 用例 | 设置 | 必须结果 |
|---|---|---|
| `CHECK-001` | `required_checks={feature, static}`；覆盖矩阵仅关联 `feature`；`static` 未声明为 `global_required` | **冻结失败**；存量畸形 Contract 也必须拒绝交付，不能跳过 static |
| `CHECK-002` | 同上，但 `static` 显式声明为 global；feature 通过、static 失败 | 结构合法，但正式交付必须因 static 失败而拒绝 VERIFIED |
| `CHECK-003` | 功能检查通过，global lint 证据缺失或被 skip | 缺失/跳过不得 VERIFIED |
| `CHECK-004` | 任一必需需求引用不存在/已停用的 check | 冻结失败，交付时重新防御性检查 |
| `CHECK-005` | 功能 + 静态必需检查均可信通过，所有需求具备充分覆盖且最终快照一致 | 可进入 OutcomeCommit；但**仍非**直接 VERIFIED，必须经最新控制状态 CAS |
| `CHECK-006` | 仅一项 Agent 自增检查满足需求，已注册必需 check 不存在或失败 | 不得通过，防止补充检查取代强制项 |

不变量：`required_checks = mapped_required_checks ∪ explicitly_global_required_checks`，其中 `mapped_required_checks` 来自**冻结的、合法且经注册**的强制需求覆盖关系。系统必须先检查必需全集的证据，后检查需求覆盖；不能让 `coverage_sufficient=True` 缓存值成为权威。

## 0.2 P1-H：验收与控制事件在同一存储线性化

| 用例 | 故障/并发时序 | 必须结果 |
|---|---|---|
| `RACE-001` | Grader 最后一个 required check 通过 → 暂停在 OutcomeCommit 前 → 持久化接纳新增需求 → 恢复旧 commit | `control_epoch` 不匹配/待分类，旧成功拒绝；后续 successor 新 Contract |
| `RACE-002` | Grader 通过 → 暂停 → 持久化接纳 Cancel → 恢复旧 commit | 旧成功拒绝，紧急停止/取消按可信状态机处理 |
| `RACE-003` | OutcomeCommit 已原子成功 → 才持久化接纳新的需求/取消 | 旧版本历史交付保留；新需求开启下一版本，取消已结束 Run 不伪造撤销 |
| `RACE-004` | 新指令已入持久 ControlInbox 但仍在 LLM 分类中 → 旧完成尝试提交 | 一律阻断旧完成；不能等分类结果到达才设置 epoch |
| `RACE-005` | 两个终止协程与同一个请求的重传同时发生 | `DeliveryRecord(run_id)` 唯一；事件 ID 幂等；最多一个 Outbox 成功事件 |
| `RACE-006` | 执行偏好（非契约变更）在评测期间被接纳 | 旧 PreparedDecision 失效；新决策可合法复用未失效的可信验证证据，不强制重跑全部测试 |
| `RACE-007` | Grader 通过后工作区 Writer epoch 改变/封存对象失效 | 即使控制 epoch 不变也拒绝旧成功；复验最新对象 |
| `RACE-008` | 客户端请求先在网卡到达，但 Delivery 事务先提交，之后才形成 durable admission | 对外序列以**事务提交**为准；未获正式接纳的消息不能倒写历史；响应须区分收到/接纳/应用 |

**测试要求**：不同数据库连接、屏障/故障注入重排、提交前故意暂停；SQLite 使用真实 `BEGIN IMMEDIATE`/独占写者语义，不能用 Python 线程锁假装多个进程/连接上的原子性。`Run.status`、`DeliveryRecord`、Event Journal、Outbox 在事务失败时必须全部回滚；同一 `ControlInboxEvent` 的重复请求不得增加控制修订。

## 1. 总览与可执行修复映射

| Review | v1.3 根因 | v1.4 最小决定 | 规范位置 | 阻断阶段 |
|---|---|---|---|---|
| **P1-A：同进程判定遭篡改** | 只读测试文件/Node ID/退出码仍无法约束内存中断言与 pytest Hook | **Trusted External Grader 与 DUT 进程、OS 权限隔离**；把同进程 pytest 降为诊断证据 | 技术 §1.2.1、§13.1.1、§13.1.3；架构 §2.1、§10.1 | **S1 / VERIFIED 上线前** |
| **P1-B：Broker 回复丢失** | IPC 未冻结原 ToolCall 请求身份、语义 hash 与结果查询 | `operation_key` + `request_semantics_digest` + Broker 持久化 `OperationLedger`；`SUBMIT / GET_OPERATION`，同键异参拒绝 | 技术 §5.4、§17；架构 §3、§5.1 | **S0 / 允许有副作用工具前** |
| **P2-C：SOURCE 误绑 wheel** | 每项证据无差别要求 artifact digest 等于 final artifact | `CheckSubjectKind = SOURCE/BUILD/ARTIFACT`；根据 Subject 分别验证，Contract 组合覆盖 | 技术 §13.1.2—13.1.4；架构 §10.1 | **S1** |
| **P2-D：Memory Snapshot 丢失** | Release 固定代码/通用 Skills，但个人/项目长期 Memory 实时变化 | 内容寻址 `KnowledgeSnapshotManifest` + 单向派生 `KnowledgeIndexManifest`，Run/Trial pinned，不把 Memory 更新当软件发布 | 技术 §4.4、§6.1、§13.4、§13.5；架构 §5.1 | **S2；S4 评测前复核** |
| **P2-E：Meta 题目可见性冲突** | 未区分创建优化器的人与冻结后答题的实例 | `MetaCandidateBuilder` **不见 hidden pool**；`FrozenMetaOptimizerInstance` 仅见当前 `VisibleMetaProblemEnvelope`，私有 grader 不可读 | 技术 §11.3、§18.3；架构 §5.3 | **S6 / 开放 L2 自修改前** |
| **P2-F：Greenfield 没有首次验收** | 空仓库无测试，Agent 自己生成检查无法独立约束它 | `PRECONTRACT_DISCOVERY → REVIEWABLE → FROZEN`；独立建立可观察行为、接口、覆盖矩阵、必要的外部验证 | 技术 §4.3.1、§13.1；架构 §2.1 | **S1 / Greenfield VERIFIED 前** |

### 修复背后的事实与项目自己的设计选择

- **pytest 官方文档**确认配置、收集、执行、报告由插件 hook 扩展。此事实支持“同进程判定可能受 DUT 影响”的威胁建模；**外部裁判隔离是 repo-code 设计选择**，并非 pytest 自带的安全模式。[来源](https://docs.pytest.org/en/stable/how-to/writing_plugins.html)
- **SWE-bench**在容器化环境应用候选 patch 并运行测试；[Anthropic Agent Evals](https://www.anthropic.com/research/demystifying-evals-for-ai-agents)强调隔离 trial、真正的结果检验及抗作弊 grader。[SWE-bench 评测指南](https://github.com/SWE-bench/SWE-bench/blob/main/docs/guides/evaluation.md)。这不证明 Docker/独立解释器自动实现绝对安全，强隔离还须验证 OS 权限。
- **AWS EC2 Idempotency**公开了稳定 Client Token 以及同 key 不同参数拒绝的设计；**Temporal Activities/Tasks**显示在断线、丢任务和重试之间必须考虑实际执行效果。Broker 本地 Ledger 是 repo-code 自定义实现，不是对任意外部系统的 exactly-once 保证。[AWS](https://docs.aws.amazon.com/ec2/latest/devguide/ec2-api-idempotency.html) · [Temporal](https://docs.temporal.io/tasks)
- **LangGraph Persistence**区分 thread checkpoint 与跨线程 Store；repo-code **额外提出 Run/Trial 快照 pin** 与知识源/index 的哈希依赖。[LangGraph Docs](https://docs.langchain.com/oss/python/langgraph/persistence)
- **自适应隐藏评测**即使只公开反馈，也可能产生渐进过拟合；repo-code 继续沿用 v1.3 受限反馈和血缘污染规则。[Dwork et al.](https://arxiv.org/abs/1506.02629)

## 2. P1-A 外部裁判必须如何被检验

### 反例验收

| 用例 | 构造（不含实际恶意代码） | 必须得到的系统结论 |
|---|---|---|
| `GRADER-001` | DUT 返回 `0`，外部契约要求 `42`；进程内 `pytest` 显示通过且 Node ID、计数、源文件 hash 均不变 | `SANDBOX_IN_PROCESS` 报告可以归档；可信外部 Grader 比对 `0 != 42`，拒绝 VERIFIED |
| `GRADER-002` | DUT 在自身 Python 进程覆盖 `unittest.assertEqual`/pytest 报告 hook | 可信 grader 运行不同 Python 解释器 + OS 权限域，不能被覆盖；外部判定不变 |
| `GRADER-003` | DUT 试图读取/修改 grader 实现、输出目录和隐藏测试答案 | 文件系统/进程/网络权限拒绝，可信报告只由 Trusted Grader 身份写入 |
| `GRADER-004` | 库无 HTTP 服务，需执行 Python 函数 | 通过沙箱内固定序列化 adapter 获取观测输出，外部裁判核对；适配器本身不被视为可信判定器 |
| `GRADER-005` | 关键需求缺少可观察 oracle，而所有项目 pytest 均通过 | `outcome=PARTIAL, verification_reason=INSUFFICIENT_EVIDENCE` |

**不变量**：测试环境、代码、外部行为输入、真实外部输出、可信裁判版本与签署结果可追溯；DUT 不在可信裁判进程内 import。外部黑盒也不能证明所有输入的正确性，正式报告仅声明已覆盖的行为。

## 3. P1-B Broker 幂等协议与故障注入

冻结消息最小集合：

```text
SUBMIT(run_id, turn_id, tool_call_uid, tool_name, tool_spec_digest,
       canonical_arguments, expected_state_digest, requested_approval_scope_digest)
GET_OPERATION(operation_key)
LIST_UNRESOLVED(run_id)  # Host-only
```

**稳定键**：`operation_key = H(run_id, turn_id, tool_call_uid)`，`request_semantics_digest = H(tool_name, tool_spec_digest, canonical_arguments, expected_state, requested_scope, protocol version)`。首次注册与唯一约束由 Broker 事务执行，ToolCall UID 由 Host 分配并持久化。审批结果是原 Operation 后续状态，不改变已经冻结的请求摘要。

| 用例 | 故障时点 | 预期结果 |
|---|---|---|
| `IPC-001` | SUBMIT 请求尚未注册前断线 | 重新 SUBMIT 原键可登记一次，执行次数最多一次 |
| `IPC-002` | Broker 已持久化 Intent，尚未产生效果 | 恢复后 GET 得到同一 Operation；仅受认可状态机允许原 Operation 继续 |
| `IPC-003` | 副作用已完成且 Result 已提交，回复前连接断开 | 重连 SUBMIT 或 GET 返回**原 operation_id、原结果**；副作用计数恰为 1 |
| `IPC-004` | 同 key、相同语义参数被并发提交 2 次 | 由唯一约束/CAS 选一个执行所有者，不产生双写 |
| `IPC-005` | 同 key、不同参数/版本/前置条件重试 | 返回 `IDEMPOTENCY_CONFLICT`，原结果未变，无第二次执行 |
| `IPC-006` | 副作用真实发生后、提交 Result 前 Broker 崩溃 | 标记 `UNCERTAIN`，尝试通过后态、进程租约或 provider request ID 对账；不得盲重放 |
| `IPC-007` | 外部 API 不提供服务端幂等且无法确认效果 | 明示 `UNCERTAIN` / 人工处理；不得保证 exactly-once |
| `IPC-008` | 可恢复 Run 尚在，Operation 记录将到保留期 | 不允许过期删除记录/重分配同键；必要审计与恢复材料保留 |

**判定注意**：本地 durable intent 与去重保证的是**在 Broker 控制面的逻辑去重**；真正 exactly-once 外部效果需要对方幂等能力或可证明对账，没有就必须诚实标识不确定。

## 4. P2-C SOURCE / BUILD / ARTIFACT 证据组合

| 用例 | Contract | 应得结论 |
|---|---|---|
| `SUBJECT-001` | 源码仓库只交付 `.py`；要求 SOURCE 静态检查 + 外部行为检查 | 不需要 wheel digest；SOURCE+外部行为覆盖充分可 VERIFIED |
| `SUBJECT-002` | 交付 wheel；SOURCE lint 证据的 `tested_artifact` 为 `None`，并有独立 BUILD+ARTIFACT | SOURCE 证据合法，不误拒绝；验证 build/link 与真实 wheel 后可 VERIFIED |
| `SUBJECT-003` | 交付 wheel；只运行 `pytest` 检查源码 | 即使 `pytest` 为绿，也缺 BUILD/ARTIFACT；不能 VERIFIED |
| `SUBJECT-004` | BUILD 来自 S0，但最终源码变为 S1 | BUILD 关联失效，最终 wheel 不可由旧 Source/Build 证据认证 |
| `SUBJECT-005` | 实测 wheel A，但交付 wheel B | ARTIFACT 证据失效；禁止复制 wheel B 的 digest 到原检查记录 |

源码检查**不自动证明 wheel 运行行为**；产物检查也不能证明源码所有静态规则。Contract 通过 RequirementCoverageMatrix 指定哪些 Subject、最小权限级别与人工审查是必需的。

## 5. P2-D Knowledge Snapshot 与隐私/恢复用例

| 用例 | 结果要求 |
|---|---|
| `KNOW-001`：同一 Release，Run A、Run B 在知识 revision 17/18 启动 | 两次 Run 明确标记各自 Snapshot + Index，能够复现差异，而非“同代码一定同结果” |
| `KNOW-002`：Run A 执行中加入新长期 Memory | A 固定旧快照；新条目作为 Working State/KnowledgeDraft 或新 Run 的已批准快照，不隐式检索进入 A |
| `KNOW-003`：Run A 强制要求读取新 Memory | 开启继承对账工作区的 successor Run，固定新快照，不继承旧验收证据 |
| `KNOW-004`：用户撤销旧快照中某项敏感记忆的访问 | Broker 立即阻止读取，必要时停止并换快照；保留法律/策略允许的最小 tombstone，不能为实验复现保留应删内容 |
| `KNOW-005`：知识源 Snapshot 版本与 Retrieval Index 版本不匹配 | 拒绝读取或重建匹配索引；不悄悄查询实时索引 |
| `KNOW-006`：L1/L2 Trial 输入不记录 Knowledge Snapshot | Trial 无效，不能据此比较候选表现 |

哈希依赖为：**知识内容/权限 → SourceSnapshotDigest → IndexManifestDigest → RunManifest**；禁止由 KnowledgeSnapshot 再反指 Index 形成哈希环。

## 6. P2-E 冻结 Meta 优化器与题目隔离

| 用例 | 预期结果 |
|---|---|
| `META-001`：MetaCandidateBuilder 尝试枚举 meta_final 题目 | 拒绝；任何曝光记入 lineage，污染 cohort 不可再宣称未见 |
| `META-002`：FrozenMetaOptimizerInstance 在评测中收到题目公开的 Failure Pattern、初始 L1 状态和预算 | 允许；实例正常完成内层候选搜索，不因“隐藏题”四字被禁止读当前公开输入 |
| `META-003`：实例访问隐藏答案、其他题、评测器内部规则或完整 report | 拒绝/审计；实例可见的只有当前 `VisibleMetaProblemEnvelope` 与授权开发反馈 |
| `META-004`：实例在 episode A 写长期 Memory/缓存，episode B 试图读取 | 不可共享；两者独立 namespace，受污染版本不能声称同 cohort 独立性 |
| `META-005`：新旧 Meta 优化器只比较一次已知修复 | 不满足 Meta-Eval；必须比较含失败候选、全预算的完整嵌套搜索 |

**主体分离**：生成器在 meta_dev 开发；冻结版本由 Trusted MetaScheduler 按题创建实例；可信 Grader 在独立权限域判定。评测可用输入属于**当前分配题目的公开视图**，不是全部隐藏 pool。

## 7. P2-F Greenfield Contract Bootstrap

| 阶段 | 输出 | 阻断条件 |
|---|---|---|
| `PRECONTRACT_DISCOVERY` | 需求/风险及外部行为清单 | 不清楚关键业务意图，不能声称已冻结 |
| `INTERFACE_DRAFT` | CLI/HTTP/API 输入、输出、错误与安全约定 | 将尚未批准的内部实现细节当作必需外部接口 |
| `REVIEWABLE` | RequirementCoverageMatrix、外部必需检查、oracle、负例 | 必需需求没有可靠的行为验证或合法人工验收路径 |
| `FROZEN` | Trusted Host/授权审查者批准的 ContractDigest/CheckBundle | 未经批准的检查、实现 Agent 可直接弱化受保护检查 |
| `Coding Run` | 真实文件/构建/调试/测试 | Run 中需求改变不产生 successor；允许 Agent 自改验收通过 |
| `Delivery` | SOURCE/BUILD/ARTIFACT 可信证据链 + 覆盖声明 | 空集合或覆盖不足却输出 VERIFIED |

**至少验证两个 Greenfield 场景**：

- `GREEN-001`：空目录 FastAPI JWT API。冻结登录成功、非法密码、非法/过期 token、未授权访问拒绝等外部行为；实现 Agent 删掉自己的失败测试，也不能删除 Trusted Bundle 的检查。
- `GREEN-002`：用户只要求“构建一个项目”，没有接口/用途。先获取必要设计约定，或进入 `EXPLORATION_ONLY` 并明确不能 VERIFIED；不能生成一个只有 `test_imports` 的弱契约宣称完整完成。

## 8. 与上一轮 v1.3 不变量的回归关系

此前六项修复和 v1.3 约束不得因这两项修订被破坏：

1. TaskSpec/AcceptanceContract 发生验收语义变化必须 successor Run；旧审批与旧验证结果不继承。
2. 正式发布的单元是完整 `ReleaseManifest` 的原子 CAS；Run 固定已测的完整组合。
3. 生产进化代码**获批后仍在受限 Worker**运行，不在 Trusted Host 中 import。
4. Dev 与 hidden promotion/final/meta 按权限和学习血缘隔离；隐藏结果曝光后后代必须使用独立 cohort。
5. S4 首次晋升前具备最小 Complexity Governor 和 Final Holdout（G4b），安全抢占独立于普通执行队列。
6. S6 前禁止 L2 自修改；即使 S6 开放，也不能改 Trusted Gate、评测 oracle 或发布策略。

## 9. 尚未验证的实现风险

**此矩阵定义了要测什么，不表示已经测过。** 真正的实现仍需实测 OS 权限隔离（Linux/Windows）、外部 grader 抗污染、IPC 并发和崩溃窗口、长期 Memory/隐私删除与恢复、meta 试题命名空间隔离、Greenfield 需求覆盖质量。单纯 Markdown 校验、静态审查、参考公开项目的实现思路都不能替代实际故障注入。


## 10. v1.5 修复后的残余实施风险

- SQLite 的写事务只能保证**同一数据库内的原子排序**；文件系统/外部副作用需要封存交付对象、Workspace Fencing 和实际 OS 隔离，不能只靠 DB CAS。
- `ADMITTED` 必须在 durable ControlInbox 提交后才向客户端确认；在接纳之前已经发生的交付不能被新目标追溯否定，UI 必须区分已接收与已接纳。
- 不可用 `check_status=PASSED` 代替可信 `VerificationEvidence` 的 CheckDefinition/Subject/Authority/签名与最终对象绑定。
- 全部矩阵为未来实现的验收准则；参考模型单测通过也不等于 repo-code 真正的并发/安全实现已通过。
