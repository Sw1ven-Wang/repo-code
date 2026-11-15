# Memory v2.1：修复与验证

实现 revision 为 `2.1`，软件包版本仍为 1.5.1 的 Unreleased 源码。默认继续使用 Memory v1。正式裁决与每阶段证据见 `reports/memory-v2.1-remediation-2026-11-01/`，旧 v2 报告保留。

## 本轮修改

`ContextBudgetCoordinator` 在发送 API 前计量完整原生请求，分项总和等于总估计；主请求、语义压缩、巩固和只读委派共用预算规则。输出保留不少于 Provider 显式配置的上限。先回收可选检索与历史观察，再对闭合前缀做最多一次语义提取；保留合法旧摘要，最多两次打包记录。硬约束和必需协议内容仍超限时明确阻断。DeepSeek DEV 的 57 条 usage 暴露最多约 17.5% 的低估，当前该 endpoint/model 对两组共用 1.20 倍校准；实际 usage 超窗时外部运行器阻断其后续效果。该经验估计不宣称等于模型官方 tokenizer，其他 Provider 不自动套用。

历史 CSV 溢出已在原源码归档及未修版本重现：第二次提取遗漏开放问题而被拒绝，回退请求仍分配固定额度给可选观察，输入 11984 大于 11404。修复不扩大 16000 窗口，也不占用 4096 输出与 500 安全余量。

语义摘要仅从实际匹配引文的来源获取证据等级；原始用户材料及全部开放事项由 Host 保留；模型返回的 retained_open_loop_ids 仅是建议，遗漏/额外 ID 不会改变 Host 事实集合（ADR-002）。JSON/schema、引用授权、实际引文及整份无效的拒绝规则仍保留。源码或文件版本变化会使旧摘要观察显示 HISTORICAL；新纠正保留旧来源和冲突标记。过长或无引文的候选单独拒绝，整份无效不推进摘要 head。过多重复引用先全部核对授权，再保留最多八条实际支持引文；完整前缀撤销闭包仍保留。原生 reasoning 不进入提炼或可见导出。

语义提取使用有界原文片段、操作结果元数据和模型提议摘要，原始消息/工具配对不变。片段不能保证语义完整；原文可按当前 Run 的授权回取。自然语言错误、遗漏、假设和 schema 拒绝分别报告。

已知版本不匹配优先于未知条件；零检索预算返回空。Intent 包含 DEBUG/EDIT/VERIFY/RESUME。既有真实本地 embedding/reranker 不变，不增加生成式 query rewrite。

巩固仍默认只产生候选，提交时再核对真实来源 Run 的作用域与可见域。Job 与知识提交在同一事务中完成；租约重试不产生独立支持样本。实际调用中断标记 USAGE_UNKNOWN，明确未发送的 Host 拒绝单独标记 NOT_DISPATCHED，不能与未知使用量混算。

## 使用与升级

使用新 state-root；显式传入 `--memory-profile examples/memory/deepseek-experimental.json` 才启用。DeepSeek 本轮参数为 `deepseek-flash`、`--thinking disabled --max-output-tokens 4096 --temperature 0`，Key 通过 `--api-key-env DEEPSEEK_API_KEY_REPO` 读取，不写入源码。任务 Docker 镜像使用 `examples/sandbox/validation-config.json` 中的固定 digest。

Host 自动将源码实现摘要绑定到 profile。沙箱 environment identity 现在递归包含 memory 子包，旧 G4 环境身份不能认证这些新文件。旧 v2 Run 的 profile digest 不会被重写；新实现不能直接接着解释其上下文，须创建新 Run。显式携带旧 implementation digest 的配置会拒绝启动。只读查看旧 manifest/证据可使用不启用新 profile 的当前 Host。旧 codec 及撤销恢复屏障保留，不能用旧二进制写 schema 17。

模型/工具表的 conversation rebase 许可只适用于实际验证的参数与工具表。标准评测关闭共享学习，使用只读工具表；该表需要独立协议证据。不得把旧完整工具表的许可自动移植过去。

操作方可调用 `app.memory.disable(reason)` 停用当前 profile：新 Run 的 pin 和待发送上下文被阻断，已有 manifest 保持可读，默认 v1 不受影响。该接口不开放给模型。

Windows/Docker 运行器使用短的独立私有 state-root，并在付费调用前测试隔离工作目录的读写。一次 DEV 实验观察到 drvfs/9p 的 EIO；后续长、短路径探针均通过，故不将路径长度断言为已证实根因。预检失败必须记录 INFRA_ERROR，不能当成模型任务得分，也不能静默从分母删除。

## 评测与回滚

旧 E2/E3 只作 DEV。独立 PROMOTION/FINAL 使用新任务和冻结输入，在预算明确批准前不运行。新任务逐题独立克隆冻结 Host 模板及私有状态，不读取前题新增知识；受信签名身份属于本轮新建的评测权威，私钥不进入公开源码包。

没有通过所有门禁时仅可裁决 EXPERIMENTAL_ONLY 或 REJECTED，不改变全局默认，不自动批准或激活 Release。回滚保留新状态审计，以当前兼容 Host 为新 Run 选择 v1；不要修改旧签名、旧快照或旧门禁结果。

## 调用次数覆盖与 artifact 分页修复

2026-11-01 用户明确要求“不限制调用次数”。新实验可显式使用 `Budget(model_calls=None)`，辅助调用配置 `auxiliary_calls=None`，DEV 脚本加 `--unlimited-calls`。默认配置仍有调用上限；显式无限模式不受旧任务累计 1000 次调用的合计屏障限制，但保留工具、时间、token、上下文、权限和验收门禁。旧的 40 次上限实验不重标为通过，新对照两组使用相同设置。

真实 CSV 轨迹暴露了 escape-heavy JSON 的 artifact 包装链。artifact.read 现在按原始 bytes 分页，返回稳定 source_artifact、offset、next_offset 和 returned_bytes；完整序列化响应低于 Broker 外置阈值。上下文预览重用已有授权原件，避免把 slice 的 JSON 外壳重复外置，保留撤销闭包。

影子迁移的源库使用只读 SQLite 连接，不再调用 Store 初始化；实际字节哈希回归验证源数据库不变。旧初始化路径仅改变库文件元数据也会被此检查发现。

## 完成响应的 JSON 错误与输出截断

正常结束的 tool_calls 若携带非法 JSON，Host 将保留原始参数于私有协议历史，并返回 INVALID_TOOL_JSON 结果；Broker 对 argument_error / raw_arguments 显式拒绝注册，不能将空占位参数执行为工具动作。原始坏参数不进入语义记忆。模型须生成下一份合法调用；不自动修补 JSON，不执行 eval。

length 响应的文本和工具调用均不进入有效执行记录，实际 usage 仍结算。Host 在控制/撤销 fence 核对后发送缩小下一步的恢复提示，继续使用同一输出上限及 Run 资源预算。压缩/巩固提案仍拒绝 length。真正 SDK 异常保留错误类型、HTTP 状态及已知 usage，未知 usage 继续保守预占。

完整独立评测运行器为 scripts/validate_memory_e2e.py；私有任务 bank 另行提供，原件冻结模板不含先前 trial 的结果或学习。PROMOTION、FINAL 各只 claim 一次。用户可明确要求 G3 未通过后仍测 FINAL；此时只完成质量测量，G4b 门禁仍拒绝，不自动批准或激活。

进程结果的预算预览优先展示 stdout/stderr，各保留有省略标识的开头和结尾；退出码、错误标志、源码版本仍独立保留。避免环境元数据挤占全部可见结果。完整原文和原始配对消息不变，可按授权回取。

语义辅助请求也执行最多两次确定性打包：先保留完整显示；超预算时仅回收可按原 source_id 回取的版本映射、长引文和旧 Claim 文本，不删除任何闭合来源或 Host 开放事项，保留退出码/错误标志。一份提案最多一次模型调用；必需结构仍超预算则明确阻断，不扩大窗口。

对支持 JSON mode 的适配器，压缩和巩固显式发送 response_format=json_object，完整请求预算与调用日志计入该字段；主任务工具调用不启用此格式。严格语义 schema、来源/引文/epoch 校验仍在 Host 执行，JSON mode 不提供事实或授权认证。不支持该能力的适配器继续按原协议返回文本并严格校验。

压缩协议进一步使用 quote_id：模型选择 Host 提供的短编号，Host 回取实际来源和精确引文，再执行完整校验；无需生成器复制带转义的代码或 message:数字 字符串。额外支持只能选择已有编号，未知编号整份拒绝。旧文本提案仍可解码并接受原有严格来源/引文校验，不自动修复非法 JSON。
