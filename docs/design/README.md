# repo-code Design v1.5 — 第三轮 Review Remediation

**日期**：2026-10-30  
**状态**：`DESIGN_REVIEW_V1.5`（只完成设计与协议修订，未实现项目代码，未运行端到端安全/性能实验）  
**范围**：修复最新 **2 项 P1**（必需检查全集、交付与 Steering 竞态），同时继承 v1.4 的 2 项 P1 + 4 项 P2、v1.3 的五项 P1 与配套约束。

## 文件导航

- [`01_repo-code_Architecture_and_Business.md`](01_repo-code_Architecture_and_Business.md)：产品定位、Greenfield 验收建立、L0/L1/L2/Trusted Control 平面、业务闭环、阶段退出门禁。
- [`02_repo-code_RSI_Technical_Design.md`](02_repo-code_RSI_Technical_Design.md)：接口契约、可信外部裁判、稳定 Broker Operation、分层 Subject 证据、知识快照、Meta-Eval 身份隔离、RSI 与完整 Release 发布。
- [`03_Review_Fix_Matrix.md`](03_Review_Fix_Matrix.md)：历次 Review 与本轮 `CHECK/RACE` 必需反例及故障注入矩阵。
- [`04_Contract_Reference_Tests.py`](04_Contract_Reference_Tests.py)：第三轮 P1 最小 SQLite 事务/契约参考模型测试（**非生产实现**）。

## v1.5 新增的两条硬规则

1. `required_checks` 和 `RequirementCoverageMatrix` 不能混用：冻结时登记所有必需检查，未映射检查必须显式标记为 global；交付时**先查必需检查全集，再查需求覆盖**，二者都通过仍需最终提交事务。
2. Grader 只生成 `PreparedDecision`；正式 `DeliveryRecord` 与 `Run.status=SUCCEEDED` 仅由 `OutcomeCommit` 事务生成。任何 **先被持久化接纳** 的 Steering/Cancel 都提升 `control_epoch` 并使旧决策过期；提交先成功则旧交付只作为历史版本保留。

详细反例与并发注入设计见 [`03_Review_Fix_Matrix.md`](03_Review_Fix_Matrix.md) 中 `CHECK-001..006` 与 `RACE-001..008`。

## v1.4 的六条关键决策

| 缺口 | 决策 | 对应阶段 |
|---|---|---|
| P1 · 同进程 pytest 可被修改判定 | Trusted External Grader/OS 隔离，同进程通过仅作诊断 | S1 |
| P1 · Broker 断线回复后重复操作 | 稳定 Operation Key + 语义参数 digest + 持久 Ledger + GET_OPERATION + UNCERTAIN 对账 | S0 |
| P2 · SOURCE / wheel 证据误绑定 | Contract 选择 SOURCE / BUILD / ARTIFACT 的真实证据组合 | S1 |
| P2 · 长期 Memory 没有固定输入 | Run/Trial pin Knowledge Source Snapshot + 匹配 Index；不改变软件 Release | S2 |
| P2 · Meta-Eval 谁能读题含糊 | Builder 不见隐藏 pool；冻结 Instance 见当前题公开 Envelope；私有 grader 不见 | S6 |
| P2 · Greenfield 不能建立检查 | 先形成外部行为/接口/覆盖矩阵，独立注册必需检查并冻结，再运行可 VERIFIED 任务 | S1 |

### 重要区分

1. **项目内 pytest 通过**不等于**关键功能已经受到独立可信验证**。可靠报告应陈述测试权限等级和覆盖范围。
2. Broker Ledger 解决**本地重复请求识别**；不能替不支持幂等的外部 API 保证 exactly-once。
3. **Knowledge Snapshot** 属于运行时输入，不属于每次软件版本发布。敏感记忆的撤销优先于快照固定。
4. **`INSUFFICIENT_EVIDENCE` 是非成功的原因码**，配合 `outcome=PARTIAL` 使用。
5. 本包中的代码示例与状态机均是拟定接口，不是已完成的软件实现。任何参考级单测仅说明示例模型的逻辑，不代表真实 Broker/Host 已通过并发或安全测试。

## 参考来源

- [pytest 插件与 hooks](https://docs.pytest.org/en/stable/how-to/writing_plugins.html)
- [SWE-bench Evaluation Guide](https://github.com/SWE-bench/SWE-bench/blob/main/docs/guides/evaluation.md)
- [Anthropic: Demystifying evals for AI agents](https://www.anthropic.com/research/demystifying-evals-for-ai-agents)
- [AWS EC2 Idempotency](https://docs.aws.amazon.com/ec2/latest/devguide/ec2-api-idempotency.html)
- [Temporal Tasks](https://docs.temporal.io/tasks)
- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [Adaptive Holdout Reuse](https://arxiv.org/abs/1506.02629)

> 以上来源提供参考机制，**不能视为那些系统已经实现 repo-code 的完整信任/评测/发布协议**。

- [SQLite 官方隔离说明](https://www.sqlite.org/isolation.html)
- [SQLite 官方事务协议](https://www.sqlite.org/lang_transaction.html)
- [Temporal Python Workflow Message Passing](https://docs.temporal.io/develop/python/message-passing)
