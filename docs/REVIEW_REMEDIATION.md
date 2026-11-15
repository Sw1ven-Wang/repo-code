# repo-code v1.5.1：两份 Review 的选择性修复

日期：2026-10-31。审查依据是用户提供的 `review1.5-1.txt`、`review1.5-2.txt` 以及原始 v1.5 源码/设计。本文记录本轮实际改动，不修改原设计要求来掩盖缺口。

## 1. 筛选原则与复现

优先处理具有最小反例、直接影响用户代码/交付/晋升结果的缺口。原实现的 132 项 Linux 测试可以通过，但新增七项安全行为断言在修复前全部失败；因此原测试数不能作为七项问题不存在的证据。

第二份 Review 的 F1–F7 均被采纳，并扩展正向与并发控制测试。报告中的 Windows 36 passed/96 skipped 属于那次独立环境的结果，不否认原 Linux 结果，也不能替代本次支持环境复测。

## 2. 第二份 Review：具体修复

| 编号 | 处理 | 实际机制与代码 | 回归重点 |
|---|---|---|---|
| F1/P1 | 修复 | `runtime.file_effect_guard/finish_control/settle_controls`；`Mutator.apply` 每个 forward effect 复查；Broker 结果结算唤醒 drain | before-effect 取消无写入；批次中断补偿后终止；效果后、回执前只进入 pending；真实独立进程 Cancel；Steering 不复活已取消 Run |
| F2/P1 | 修复 | `EvolutionEngine.optimizer_release/_optimizer_call`；Meta 分别固定 Coding 基线和候选优化器，记录实际组件 digest | 候选返回 inconclusive 时内层不再走默认实现；隔离 Python L1 的 diagnose/propose 标记实际进入嵌套搜索 |
| F3/P1 | 修复 | `release_content_identity`、内容/候选/Release 血缘、事务化 `cohort_reservations`，schema16 保留旧曝光 | 换 ID、换构建说明、换 cohort 名称、改题 ID 不重置资格；并发仅一次成功；L1→L2 污染传播；旧 schema 迁移 |
| F4/P1 | 修复 | `Check.permitted_authorities` 冻结；required 全集逐项验证；Diagnostic 仅补充 | 全局 diagnostic 在冻结时拒绝；即使签名合法，低可信全局证据也不能放行 |
| F5/P1 | 修复 | `mcp_request` 发送阶段账本与 Operation 绑定；发送后未知断连标 UNCERTAIN，Run 对账阻断 | EOF/timeout/坏 JSON/partial drain/cancel；已关闭连接与发送后区分；真正隔离进程写入 `/tmp` 后 EOF |
| F6/P2 | 修复 | `origin/origin_digest` 与 `inherited_source` 分离，successor 继承最初用户目标 | 多代 successor 合入原目录；原目录被人工修改则拒绝，不修改旧内部副本 |
| F7/P2 | 修复 | `Mutator.plan` 将批次全部 source/destination 与历史 changed 合并核算 | 限额失败时无 Journal/文件效果；move 两端计入；历史+当前批次总额 |
| G4 范围 | 扩展并设强门禁 | 新 `regression.py` 固定 12 组候选绑定回归；`activate` 要求 suite/env/full manifest 完整 | 逐一调用全部实际组件；畸形候选被拒；缺少案例、别的组合或旧探针报告不能晋升 |

测试主要位于 `tests/test_review_v151.py` 与 `tests/test_review_v151_extended.py`，也保留所有原有实现测试。

G4 的 12 组为：组件 ABI、原生隔离边界、L0 空目录编写/修正/运行/验证、受保护路径、stale edit、patch 总额、取消写入栅栏、Operation 幂等、每检查 Authority、交付控制竞态、知识撤销、只读子任务。每个候选使用干净 Host 状态并导入实际 Release；全部案例需通过，报告绑定组件、环境和评测代码摘要。

这是一组明确、可执行的有限回归，不宣称穷尽所有兼容性输入、沙箱攻击或隐藏评测漏洞。真实部署还需要目标环境安全审计和任务质量测试。

## 3. 第一份 Review：采纳、补齐与保留边界

| 意见 | 判断与本轮处理 |
|---|---|
| 真实模型验证缺失 | 成立，属于关键验证缺口。新增五任务 opt-in runner 与机器可读预检，禁止 Fake fallback；本次没有凭据/SDK，真实调用仍为未执行，不能“修成已通过”。 |
| 没有多维复杂度度量 | 这一表述不完全符合源码。已有 AST 分支、依赖边/环、模块、Prompt/Schema/知识 token 估算、角色数、组合 delta 和重复 AST 提示。本轮保留度量器，增加膨胀拒绝、SIMPLIFY/MERGE/REMOVE 候选真实执行回归。没有声称自动语义级去重或长期收益已证明。 |
| 错误归因/触发过于简单 | 部分成立。本轮让症状保存至少两个事件和 action/state 指纹；未知/瞬时故障、新证据与修改打断 streak。Triage 要求可核对证据、跨 Task 支持，无未解决矛盾/冷却，并记录理由、风险和搜索上限。诊断缺少目标、证据、假设或负例检查时拒绝；`inconclusive` 不生成候选。仍是保守规则+受证据约束假设，不是成熟的因果推理系统。 |
| 隔离安全未全面验证 | 成立的边界，不通过“保证绝不逃逸”解决。新增实际隔离 MCP 反例和候选 G4；Docker、Windows/macOS 专项未执行，完整安全认证仍无。 |
| Knowledge 撤销/备份 | 补测试：归档后旧快照仍可追溯，撤销后旧快照阻断，撤销后备份恢复仍阻断。撤销前备份与历史 blobs 的全面擦除暂不实现；明确写入升级/安全说明。 |
| L2 真实收益、Dense、图形界面、跨平台 | 不在此次修复范围。L2 实现绑定缺口 F2 已修，真实改进收益仍需独立模型实验；不增加无证据的技术栈。 |

## 4. 新旧测试的区别

- 原包全部测试的成功，表示原有断言通过，不覆盖本次新反例。
- `reproduced-v150` 日志中七项失败表示新安全断言发现了旧问题；不是产品验收通过。
- 修复后使用相同断言加扩展控制测试，预期为错误操作被阻断、正确操作仍可完成。
- 原有诊断测试调整为“诊断可供开发参考，但不可注册为必需可信检查”，并新增全局 Authority 反例；没有用删除测试掩盖不兼容。
- 原有 Meta 合成夹具刻意使用相同任务，仅在外层明确的 `synthetic_integration` 标识下传递到内部；真实非合成评测不允许这种重叠，成绩仍禁止默认生产晋升。
- 原 13 项设计参考模型仅补 SQLite 显式关闭，断言不变，独立运行不混算实现数量。

## 5. 未验证即未关闭

本轮仅认定上述反例在当前 Linux 支持环境下被修复和回归验证。**不据此宣称完成 S0—S6 全部退出门禁、达到生产就绪或优于商业 Coding Agent。**

当前测试数量、重复轮次、安装包/解压复测及命令日志以 `../reports/TEST_REPORT.md` 为准。真实 API 预检与五任务运行结果严格分开记录。
