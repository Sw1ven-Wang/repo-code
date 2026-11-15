# 任务实施权限与验收合同

`workspace_write` / `process` / `delegate` 是可信 Host 对工具的预授权；是否注册内部冻结 Contract 是独立的验收事实。没有内部 Contract 不代表只读，也不代表自动获得写权限。

旧上下文将未注册 Contract 展示成 `Task contract: {"mode":"EXPLORATION_ONLY"}`。在订阅 sol/low 的四题运行中，模型均将该标记理解为禁止修改，虽然 Host 实际授权了写入。现在 legacy ContextEngine 与 Memory v2 使用同一个 `task_authority_context()`：

- 没有内部 Contract 时显示 `NO_FROZEN_INTERNAL_ACCEPTANCE`，明确它限制 VERIFIED 声明，而不撤销已授权的实施工作。
- 显示实时 Host execution permissions；任务要求实施且 workspace_write=true 时，应实际修改工作副本，再运行已授权的检查。
- 保留冻结 Contract 的 requirements / interface / forbidden_paths / protected_paths。
- 未授权操作仍由 Broker 的原审批流程处理；delete/move、外部副作用、路径/版本/预算等原门禁保持有效。
- 未注册内部验收时，模型结束后的原生状态仍为 PARTIAL，不产生 VERIFIED 交付；2026-11-05 起本地修复版将主动 final 的结束原因改为 `CANDIDATE_READY_EXTERNAL_EVALUATION: no frozen internal acceptance`，避免与只读模式混淆。历史记录保留 `EXPLORATION_ONLY`，解析新旧报告时应按各自版本处理。外部判题器存在时，其成绩独立报告。

这项修改不调整模型、推理强度、上下文预算、Memory 学习策略或判题规则。现有历史 Run/状态不迁移；源码变化后应使用新 state-root 复验。
