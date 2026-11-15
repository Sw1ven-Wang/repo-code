# repo-code v1.5.1 实现映射与范围

日期：2026-10-31。本文描述随源码交付的实际实现；设计基线不等同于实现证明。真实执行结果见 `../reports/TEST_REPORT.md`。

## 1. 产品及执行方式

本版为独立的 Python CLI/SDK Coding Agent，不与 CareerForge 或任何教学产品耦合。工作对象是用户项目副本，可以从空目录开始，产生文件、运行命令、调试、执行注册检查、形成最终交付。原项目只有经过显式 `integrate --confirm` 并通过版本检查后才会改变。

程序采用模块化单体 Host 和受限执行域，不额外引入 Redis、微服务或常驻数据库服务。正常运行、评测和元评测共享相同的 `RepoCode` composition root。

```text
CLI / Python SDK
       |
RepoCode → CodingAgent → Broker → Workspace / Sandbox / MCP
       |         |          |
    Runtime   Context     Operation Ledger
       |         |          |
       +------ SQLite / private content-addressed artifacts
       |
External Grader → PreparedDecision → atomic Delivery commit

DEV events → Observer → FailurePattern → EvolutionEngine
       → bounded search → SuiteEvaluator → Releases CAS

MetaEvaluator → fresh frozen L2 → actual L1 search → actual L0 trials
       → private external score → full release gates
```

## 2. 源码职责

| 模块 | 职责 |
|---|---|
| `service.py` | CLI/SDK 统一装配，固定 Provider 配置，单 Run 执行锁，控制入口生命周期 |
| `agent.py` | 动态模型—工具循环、审批暂停、恢复、验收请求、版本化策略执行 |
| `providers.py` | OpenAI/Anthropic 流协议，消息适配、usage、严格工具参数、ScriptedProvider、调用计量 |
| `runtime.py` | Task/Run、Steering/Cancel、预算、验收准备与原子交付、successor、保守恢复 |
| `storage.py` | WAL/FULL SQLite、事务、内容寻址私有材料、签名、事件及 outbox |
| `models.py` | Contract、Task、Operation、Evidence、Release 和 Candidate 数据契约 |
| `workspace.py` | 规范化路径、快照、版本化变更、Journal、多文件补偿与崩溃对账 |
| `broker.py` / `ipc.py` | 统一工具执行、稳定请求去重、审批、鉴权 loopback IPC、控制通道 |
| `sandbox.py` / `sandbox_entry.py` | native/Docker/local 后端、受控进程、限制、取消、归属记录 |
| `acceptance.py` / `bootstrap.py` | 独立契约建立、全量必需检查、外部判定、三类证据与覆盖 |
| `context.py` / `knowledge.py` | 历史投影、完整消息组折叠、BM25、项目作用域、知识快照、撤销 |
| `mcp.py` / `skills.py` | 受限 stdio MCP、Skill 导入导出；不是高权限插件 import |
| `observation.py` | 轻量失败症状聚合、DEV 水位、优先筛选、持久化单问题任务队列 |
| `evolution.py` | 证据归因、定向候选生成、开发集修订、Skill 候选提取 |
| `evaluation.py` / `regression.py` | 干净工作区配对试跑；完整候选绑定的 G4 固定回归与 G4b；缺项或错误版本报告不可晋升 |
| `governance.py` | 全 Release CAS、硬复杂度预算、审批/回滚、Holdout 和污染血缘 |
| `evolved.py` | 发布前后相同的受限 Python 策略 ABI；有限模型请求 |
| `meta.py` | 完整 L2→L1→L0 嵌套评测、全成本预算、隔离实例及私有审计归档 |
| `delivery.py` / `maintenance.py` | 导出/合入、离线完整性检查、私有备份、全新目录恢复 |

## 3. S0—S6 交付映射

| 阶段 | 实现 | 可执行验收位置 |
|---|---|---|
| S0 | 固定协议、Store、Run、Broker、稳定 Operation Key、断线查询、权限和隔离 | `test_broker_workspace_ipc.py`, `test_acceptance_and_races.py` |
| S1 | 动态编码、Greenfield Contract、函数/CLI/ASGI 外部检查、SOURCE/BUILD/ARTIFACT、交付 | `test_agent_and_artifacts.py`, installed-wheel 演示 |
| S2 | 折叠与回取、项目记忆隔离、Snapshot/Index pin、撤销、流适配 | `test_context_knowledge.py`, `test_providers_mcp.py` |
| S3 | 审批恢复、独立 CLI Cancel、后台进程、受限子 Agent、隔离 MCP | `test_cli_maintenance_and_crashes.py`, `test_providers_mcp.py` |
| S4 | 症状聚合、定向候选、开发/晋升/最终门禁、最小硬复杂度检查、完整 CAS | `test_evolution_governance.py`, `demo` |
| S5 | 增长度量、重复 AST 提示、TUNE/REPLACE/SIMPLIFY/MERGE/REMOVE/ADD、有引用保护的候选退休 | `test_cli_maintenance_and_crashes.py`, `governance.py` |
| S6 | 冻结 L2 的完整嵌套搜索，Builder/Instance 隔离、总预算、默认禁用自修改、Meta Gate | `test_meta_nested.py` |

阶段表只列实现路径和对应测试，**不是 S0—S6 全部退出条件已满足的结论**。真实模型任务质量、演化泛化收益、目标部署平台隔离及压力验证仍未完成。本轮修复细节见 `REVIEW_REMEDIATION.md`。

原设计附带的 13 项参考模型测试保留为 `tests/design_reference.py`，通过 `python tests/design_reference.py` 独立运行。它们不是当前实现测试的替代品，也不与实现测试数量混算。

## 4. v1.3—v1.5 Review 的实际落点

### 验收不能遗漏全局必需检查

`validate_contract()` 对覆盖矩阵引用、独立必需全集、全局检查、重复 ID、合法 Subject 和受保护状态进行校验。`delivery_verified()` 首先遍历 `required_checks` 的全集，逐项验证签名、Subject 绑定和冻结的 `permitted_authorities`，然后检查需求覆盖。未在需求矩阵中的静态检查必须列为 `global_required_checks`，同样必须执行通过。

### 原子成功提交与新指令排序

测试执行产生 `PreparedDecision`。最终 `Runtime.commit()` 在文件写锁与 `BEGIN IMMEDIATE` 事务内，重新检查 Run revision、control epoch、writer epoch、状态、未决控制、待审批/未知 Operation、实际源码和固定知识输入。Delivery、Run 终态和 outbox 同事务提交。两个真实 SQLite 连接上的控制/交付竞态已经测试。

执行偏好可在当前任务接纳后重新进行必要验证；需求/范围/验收变化冻结新 Contract，创建 successor Run。旧交付若先提交，保持历史有效；后续指令不重写它。

### 独立裁判与实际被测对象

预期答案留在 Host。被测代码只能在受限域返回输出；Host 比较输出并签名。测试定义只读并不被当成内存隔离的替代。普通 pytest/自报“通过”作为诊断。关键行为检查和人工验收分别具有独立 Authority。

函数/ASGI 适配器仍是非可信域的一部分。可信判断证明的是观察到的外部协议结果，不是“无论提交程序如何篡改 Python，都能证明内部函数真值”。强攻击场景需要契约定义清楚的外部行为、足够覆盖、受保护的独立环境和额外安全审计。

SOURCE 只绑定源码；BUILD 记录源码到实际产物；ARTIFACT 在不含源码实现的副本中测试产物。不会向之前的源码测试补填 wheel digest 来伪造测过产物。

### 完整 Release、知识输入和生产权限

发布是整份 ReleaseManifest 的 CAS，而非多个组件独立拼接。任何基线变更使旧评测不能直接晋升；回滚也恢复完整已批准组合。每个 Run 固定知识快照、索引、模型配置和 Release。

可进化 Python `handle()` 永远通过 `EvolvedWorker` 运行在隔离进程。不能因批准就 import 到持有凭据和数据库的 Host。组件只能返回本接口允许的 JSON 或有限的模型请求；权限与最终副作用仍由 Broker 判定。

### 隐藏评测与 Meta 身份

开发报告可以参与候选修订；隐藏报告在可信 Store，Builder 不获得题库和逐题结果。测试实例只能获得当前任务允许的公开数据。曝光沿候选父关系、知识来源和优化器依赖传播；同题改 ID 不能恢复独立性。语义近重复仍依赖数据策展，未实现通用数据污染检测器。

Meta-Eval 实际运行候选优化器、L1 搜索、L0 候选试跑和隐藏评分；没有用人工填入“成功分”替代嵌套链。每个 episode 的原始 DB/Artifact 在清理临时实例前保存到私有审计归档，不能因工作区删除而丢失失败候选证据。

## 5. 自进化的实际修改范围

L1 可修改固定组件槽位的配置或受限 Python：Memory 召回策略、Context 预算、Retry/Planning 行为、工具描述、Skill 索引、只读 Delegation 策略。L2 可修改 `l1_optimizer/l2_optimizer` 的配置、提示和内部实现。槽位接口不能自主扩展；任意新增的高权限服务、依赖、数据库权限或可信接口升级不在自修改范围。

`REMOVE` 指恢复原槽位的可信默认实现，不是删掉权限或验证代码。架构级变化可以在受限 `handle()` 内组合多个纯计算步骤，但不会自动新增永久进程或新管理层。

候选使用内容寻址，开发集修订有明确次数/模型调用/时长上限。Meta 总账统计嵌套失败尝试及全部模型调用。低质量候选拒绝后保留记录。自动提炼 Skill 必须经 Release 门禁，不可通过人工知识导入命令直接生效。

## 6. 明确的实现取舍和未验证范围

| 项目 | 本版状态与边界 |
|---|---|
| CLI/SDK | 可用；包含多进程控制、明确审批、导出合入；未做 React/VS Code 图形界面 |
| 真模型 | 两种原生 SDK 适配实现并通过协议测试；本次预检缺少所需 SDK/API Key；新增五任务 opt-in runner，但真实 API E2E 未执行 |
| native | Linux root/chroot/setuid/libseccomp 实测；不构成完整安全认证 |
| Docker | digest-pinned、无网络、低权限、资源限制后端已写；当前无引擎，未实测 |
| Windows/macOS | 代码有显式可用性判断；未宣称本机沙箱和全部行为已验收 |
| 依赖安装 | 由操作者预先装入运行时或镜像；不支持让任意任务联网自动安装软件 |
| MCP | 隔离 legacy stdio 已验证；不支持所有新协议特性、HTTP MCP 或 Docker stdio 连接 |
| 子 Agent | 只读、有预算、独立消息；未开放多 Agent 并行写入和任意 DAG |
| 检索 | 文件搜索、BM25 知识、引用回取；未增加未经效果验证的 Dense/LSP/Cross-Encoder |
| 隐私清理 | 停止引用/撤销、私有备份、保守保留；没有宣称对所有历史备份实现即时隐私擦除 |
| G4 | `regression.py` 对实际完整候选运行固定 12 组回归，逐组件调用并绑定 manifest/env/suite；仓库全量测试另外运行。固定回归集不是穷尽安全审计，也不证明真实模型收益 |
| 统计效用 | 有同模型成对试跑、退化/成本门禁；当前默认阈值是工程试验配置，不输出未经数据支持的置信结论 |
| 性能 | 已记录测试/演示用时；未做生产并发压测或真实模型能力排名 |
| 元进化收益 | 完整链路已运行；“优化器真正学得更好”仍需要独立真实模型实验 |

这些边界不会用 local 裸执行、虚构评测结果或静默跳过来替代。无法满足关键验证时，程序保持未验证、阻塞或拒绝晋升。

## 7. v1.5.1 新增契约

1. `CONTROL_PENDING` 不代表已停止。文件 forward effect 在 writer lock 与短 SQLite 写事务内复查 epoch；取消接纳与该效果有明确先后。`finish_control` 同时检查进程资源与未结算 Operation。补偿结束之前不得宣告 `CANCELLED`。这不是文件系统和数据库的 ACID 事务，也没有硬实时取消保证。
2. `origin/origin_digest` 固定用户最初目录及集成基线；`inherited_source/workspace_baseline_digest` 记录 successor 继承字节。二者不混用。
3. MCP `mcp_request` 保存 PREPARED/SEND_STARTED/SENT/COMPLETED/UNCERTAIN，与本地 Operation 关联。未知外部效果保持阻塞，即使传输进程已经停止。
4. Cohort 检查与预占在同一事务内；身份覆盖全部组件、协议、worker、依赖与 gate policy，不受生成编号、构建说明或随机 Candidate ID 变化影响。旧 schema 15 升到 16 时回填历史曝光/预占，不赋予旧报告新资格。
5. Meta episode 的 `coding_base_release_digest` 与 `optimizer_release` 分离；内层 `search_config/diagnose/propose` 均调用冻结候选的 `l1_optimizer`。私有记录保留组件 digest、调用输入输出摘要和全成本账本。
6. Observer 仅产生局部症状；新证据、修改或瞬时/未知错误打断重复链。Triage 记录跨 Task 支持、证据完整性、冷却/矛盾、失败影响及有界搜索建议。规则不是经过验证的因果推理器，`INCONCLUSIVE` 仍是正常结果。
7. 已有复杂度度量覆盖 AST 分支、模块/依赖、Prompt/Schema/知识体积和角色；本轮新增拒绝膨胀、简化/合并/恢复默认候选的执行回归。不宣称实现了自动语义证明、最优结构搜索或真实费用收益。
