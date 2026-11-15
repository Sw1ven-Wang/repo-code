# SWE-bench 联网任务容器

此目录是后续实验使用的工作版 Adapter。已经完成的 DEV-2 及其源码快照仍位于
`E:\Coding\repo-code-evals\pilot2-20261101\pilot2`，不会被本目录更新覆盖。

任务容器默认使用 `public` 策略，连接专用 Docker bridge `repo-code-eval-egress`，
可以解析域名、访问 HTTPS 和下载依赖。该网络关闭同网容器互通，不发布宿主机端口。
Docker socket、Host 目录、API Key 和评测材料仍只位于可信控制侧。
本策略允许公网出站，不是目标域名白名单，也不保证阻断宿主机网络服务；
不使用 host 网络模式，不提供宿主机文件或 Docker 管理权限。

## 2026-11-05 Review 修复入口

`App` 统一使用 `TaskRuntime` / `TaskBroker`：任务创建即带通用候选保护说明；不可用的 delegate、MCP、后台 process 工具不再发布，伪造调用也在注册时拒绝。当前无学习 SWE 配置的 A/D 工具集合为 14/18，仍保留原生结构差异。正常同步执行和依赖联网不变。

将 `load_verified_policy(...)` 的可信结果通过 `App(..., verifier_policy=policy)` 传入，使写入前预检与最终导出使用同一例外规则；不向模型传评测脚本、恢复路径列表或 gold。现有 SQLFluff YAML 仍需保留原字节并独立追加，不能把改写旧预期的候选拆删 hunks 后计分。错误 YAML 在效果前确定性拒绝。

新实验可用 `audited_provider.AuditedProvider(configured_provider, fresh_destination, ...)` 包裹显式配置的后端。它保留请求/响应、共享主/辅助累计账本，并向 A/D 的 Host 状态提供 Token 余量。默认 `context=16000,total_tokens=400000,calls=120,wall_seconds=1800`；同时冻结 Release 的 context_policy，不能只改 Provider。显式 `None` 可用于用户授权的无累计上限实验，仍记录真实 Usage 和未知预留。它不自动读取或复制 OAuth，底层 provider 负责凭据隔离。

`trace_metrics.py --root 实验目录 --output 新JSON文件` 按结构统计 Git 进程请求数、命令数和 API 回合数，只创建新的派生结果。原有封存 `finalize_swe.py` 与历史分数不修改。

源码、工具 schema 和 Memory implementation 摘要改变后，必须新建 Run/state-root 并重新冻结 profile。旧原生 opaque rebase 证明不能仅替换哈希沿用。完整说明见项目 `docs/REVIEW_REMEDIATION_20261105.md`；这次修复的离线回归不等于新的真实模型成绩。

环境变量：

- `REPO_CODE_TASK_NETWORK=public`：默认，可联网。
- `REPO_CODE_TASK_NETWORK=offline`：显式断网。
- `REPO_CODE_DOCKER_NETWORK=repo-code-eval-egress`：可选专用网络名。
- `REPO_CODE_EVAL_ROOT=/eval`：可信评测数据与输出根目录；在 Windows 上映射到 E 盘。

网络策略写入 Sandbox identity、Release、预检清单及逐题 Run 记录。
预检后切换网络策略会被拒绝，需要新预检和新实验标识。模型会被明确告知是否可联网。
原生 repo-code Sandbox 及其 G4 断网策略不因本目录改变。

联网验证只创建临时任务容器，执行 DNS、PyPI HTTPS、`pip download sqlfluff==0.7.0 --no-deps`
并核对官方 PyPI SHA-256；不安装该包，不替换基线源码，不调用模型。
以下命令在可信 Linux 评测控制容器中执行，源码/输出目录映射到 E 盘，
并从本目录运行以避免导入旧冻结版 Adapter。显式加载 pytest-asyncio：

```bash
python -m pytest -p pytest_asyncio.plugin test_adapter.py test_network.py -q
```

本次验证记录：
`E:\Coding\repo-code-evals\pilot2-20261101\network-enabled-20261101\network-validation`。
此前任务容器已经清理，联网策略用于下一次使用本工作版创建的任务容器。
开始新实验时必须使用新的 `REPO_CODE_EVAL_ROOT`；不要把输出指向旧的冻结结果目录。

使用此工作版开始新模型实验之前，仍需完成冻结选题、官方 gold/空补丁控制、
环境预检与模型费用上限配置；创建联网能力不会自动发起新一轮评测。
公开测试导出修正现已整合进 `export_policy.py`、`adapter.py` 和通用驱动。
可信侧从成功 gold control 的原始官方 `eval.sh` 提取注入测试前的精确 Git 恢复路径，
绑定 base_commit 及脚本 SHA-256。仅这些公开测试允许随原始候选补丁完整导出；
不删除测试 hunks，不生成第二候选，也不修改官方评分器。缺少证据时仍拒绝已有测试修改。
未被恢复的已有测试、判题配置、控制路径、二进制及符号链接继续拒绝。
脚本、gold 和恢复路径仅用于可信导出侧，不进入模型或 Memory。

`memory_ad.py` 提供 SWE-bench DEV-2 的 A/D 驱动：A 不加载 Memory v2，D 按指定锁文件
加载；每题每组新 state-root，跨任务学习关闭，模型、上下文和工具/时间预算相同。
先运行 `preflight`，再通过 `controls.py` 的 gold/empty/baseline，最后通过 stdin
将 API 凭据交给可信 `agent` 进程。不得把密钥写入任务容器环境或实验文件。
用户要求累计 Token 不限时，本驱动关闭外部累计和费用停止线，当前 Memory 可用
`total_token_budget=None` 表示不限；单次输出/上下文限制保留。旧 16k 轮的整数哨兵保留在原冻结清单中。
联网验证和 DEV-2 对照均不构成 TEST-20 全流程验收或任意 Agent 接口的通用验收。

Docker bridge 说明：https://docs.docker.com/engine/network/drivers/bridge/

2026-11-02 验收：32 项 Adapter 回归、348 项原生回归通过（另 8 项未纳入原生筛选）；
原 PyVista 6779 字节候选完整导出，SHA-256 不变。新版 Memory A/D 在 DEV 两题各执行一次，
官方评分 A=1/2、D=1/2：两组 PyVista 均通过，SQLFluff 均空补丁。不是 TEST-20 分数。
评测记录：`E:\Coding\repo-code-evals\memory-ad-dev2-20261102\pilot2\REPORT.zh-CN.md`。
原始 Run 暴露了两项后续问题：工具预算拒绝留下 REGISTERED 请求，触发待对账状态；
PyVista D 实际输入估算误差导致预留预算超出 11 Token，触发本地保护。两者均保留原始证据。
本轮采用独立可信调度器只继续未尝试的 Run，不改固定 Agent/Release/参数、不重跑；
因此不能把本次完成测评宣称为原生 Runtime 已无故障或 Memory 已有收益。原始驱动遇到
这些停止状态仍会停止付费调度，应先检查证据，不能盲目重试。

后续 64k 修复版本：`context.py` / `memory/budget.py` 反馈校准后续输入估算，
有效已返回回复不再因未使用的预留 Token 被误拒绝；真实输入加实际输出超窗口仍阻断。
`broker.py` 的预算拒绝先提交 CANCELLED/未执行，再抛出预算错误；已执行或 UNCERTAIN
操作不受此修复豁免。旧实验快照与原始失败仍保持不变。

当前 `memory_ad.py` 使用独立的 `experiment-spec.json` 冻结 64,000 Token 上下文，
A / D-off / D-on 三组、两题各一次，共六个全新 state-root。
D-on 按 PyVista → SQLFluff 学习顺序，显式使用本次实验共享 scope；
`learning_transfer.py` 仅迁移已激活 Host 观测及必要公开证据、来源描述和撤销派生关系。
不复制 Host key、执行任务/工作区、对话历史、进程、模型提议、gold 或评分日志。
新 Host 重建索引；历史观测保留 HISTORICAL 与适用性校验，不能授权编辑或声称成功。
第一题没有前序学习；跨任务效果只可能在第二题出现，不能把六次运行当成六道题。

`regression_fixtures.py` 另支持严格证明 SQLFluff YAML 文件只追加独立回归用例：
原文件必须完整保留为字节前缀，既有键值不变，新增项限定纯数据 schema；拒绝重复键、
标签、锚点、动态对象和判题配置变更。保留完整补丁，不删除测试 hunks。
64k 实验的补充官方对照确认仅增加该用例、不修实现仍未解决任务；初始拒绝记录保留。
本轮 Agent 固定快照没有更改，后处理修正和只调度未尝试任务的记录单独归档。

64k 结果：A / D-off / D-on 均为 1/2；真实上下文违规、待对账 Run、未知 Usage 均为 0。
369 项原生回归及最终 45 项 Adapter 回归通过。D-on 第二题导入 1 条历史结束观测，
同一条观测被选入 93 次，未增加成功题数。原生语义巩固另受 64,000 字节输入上限约束，
本轮没有产生可检索的模型经验规则，不能据此证明完整语义经验迁移或长期收益。
详见 `E:\Coding\repo-code-evals\memory-ad64-learning-dev2-20261102\pilot2\REPORT.zh-CN.md`。
