# Review 1.1 / 1.2 修复

当前源码在原有 Agent、v1 折叠及 Memory v2.1 结构内修复工作证据、无进展判断和 Adapter 接入。默认模型、24k 默认上下文和权限门禁不变；本轮没有真实模型试跑，不据离线回放宣称能力或 Token 节省比例。

- A/D 共用有界 Host 工作账本：当前轮、相对公开基线的变更路径、最后写入、同版本本地验证的命令与结果、可用累计预算。新写入会使旧验证变为历史；普通 Git/ls 不能覆盖 pytest 证据。最终成功仍由可信判题决定。
- `workspace.diff` 是只读诊断工具，不开放 Git 历史、refs、可信测试或 gold。分页有界，大文件/二进制仅返回元信息；正式补丁仍由原导出器在干净 Git 基线上完整重放。
- A 的归档改为可读索引和稳定的组句柄，不再把 artifact.read/history.expand 原文反复嵌套进去。原始消息及原生协议块不改写，opaque 不进入诊断索引。
- 精确重复守卫保持；额外按版本/轮次跟踪跨工具区间与验证证据。无新证据的混批不能用 ls 清零；新页、源码变化、真实不同失败和两次带理由恢复保留。停滞只结束 PARTIAL，不制造成功或已完成轮次。
- D 给当前候选验证独立优先级，写记录和读记录分开保留；失败带命令摘要，grep 无匹配不再当作待调查的进程故障。无显式 plan 时可提出待验证事项，仍是 PROPOSED，不能更改验收。
- D 给近期相关 FACT/EPISODE 一个有界可见量，保留来源和时效，并记录实际入投影的 claim ID。字段成本是预算估计，最终仍校验完整原生序列化请求。没有增加语义压缩频率或更换检索器。
- SWE App 从任务创建起提示同一候选规则，文件/多文件 patch 和进程导入在 Host 效果前检查。改写旧 YAML 预期仍拒绝，独立追加可通过；语法错误是确定性拒绝，不产生虚假的 UNCERTAIN。最终导出检查保留，不剪裁冻结候选 hunks。
- SWE 的不可用工具在 schema 和注册层同时移除/拒绝。没有 Git 时给出 workspace.diff 的替代反馈，正常进程与依赖联网能力保留。
- `integrations/swebench/audited_provider.py` 为新实验提供所有主/辅调用共享的 Usage、保守预留与反馈。真正未知 Usage 保留预留；可信未发送证明单独记账。默认仍是 400k/120/1800；显式 null 可关闭 Token/调用累计上限而继续记账。没有用偏小估计放宽原有硬停止规则。
- Git 指标按 argv 和常见 shell 分段统计，分列进程请求、命令、API 回合。历史误计只另存派生审计，不重写原分数。补丁重放显式禁用全局 autocrlf，避免 Windows 换行影响字节证明。
- 持续任务另出 v2 题包：接口从 T1 明确，保留后续行为纠正；Queue T29/T30 对齐；dry-run 和 CLI 规格/检查一致。逐轮交付、当轮验收、终态验收和召回分开，未到达或未验收为 N/A。简化参考必须在新增反例中暴露缺口，不能凭旧 7/5 项全绿宣称完整参考或预算可达。

完整证据在 `E:/Coding/repo-code-evals/review-remediation-20261105/`。最新测试、源文件哈希、修复补丁及七个零推理历史投影回放见该目录报告。

## 新实验的冻结要求

不要修改旧 `evaluation-lock.json`、源码快照或 Run 状态后续跑旧实验。新源码和工具 schema 会改变 implementation/tool digest，必须使用新的 source freeze、profile 和 state-root。旧 opaque rebase 证明不能仅改哈希后沿用；须按新 schema 重新联调。离线 SDK Mock 和历史投影回放证明序列化/打包行为，不是新的服务端证明。

Mini10 仍没有在原预算内完成 37 轮的可信执行者正对照。新 controller 对正式长程 Memory 成绩保持 `BLOCKED_REFERENCE_OR_BUDGET_CALIBRATION`；可报告受限预算的进展，不能补造未到达的召回分数。
