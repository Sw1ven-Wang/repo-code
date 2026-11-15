# repo-code 1.5.1

可选模型后端：`--provider chatgpt-plan / deepseek / openai / anthropic / scripted`。ChatGPT 订阅仅接模型推理，保留 repo-code 的工具、Memory 与执行循环；需要独立浏览器授权。[配置与 E 盘运行示例](../../docs/CHATGPT_PROVIDER.md)。

Memory v2 实验性迭代已加入显式配置入口，默认保持原路径。[使用与迁移](../../docs/MEMORY_V2.md) · [本轮验证与灰度裁决](../OPEN_SOURCE_STATUS.md)。

Memory v2.1 修复与复验：[使用说明](../../docs/MEMORY_V21.md)。默认仍为 v1，独立质量门禁与工程回归分别记录。

> **2026-10-31：v1.5.1 Review 修复版。** 优先修复两份 Review 中可复现的运行、验收和进化评测问题。
> 本版不等于已完成所有设计阶段验收，也不宣称真实模型能力提升或生产沙箱安全认证。
>
> [本轮修复矩阵](../../docs/REVIEW_REMEDIATION.md) · [升级与状态兼容](../../docs/UPGRADING_1_5_1.md) · [实际测试报告](../OPEN_SOURCE_STATUS.md)

> **2026-10-31 中转站接入补测：** 已增加 `gpt-6.1-sol` 的 `--reasoning-effort medium` 配置及真实 API 测试入口。接入结果、复现命令与隔离环境限制见 [API 接入报告](../OPEN_SOURCE_STATUS.md)。本次修改位于源码，现有 `dist/` 安装包未重新构建。

> **2026-10-31 Docker 实际任务验证：** 已配置 Windows + Docker Desktop 的固定摘要镜像，使用中转站 `gpt-6.1-sol / medium` 完成开发、调试、文档和数据处理的 5 个 Python 任务。修复工具接口命名、参数反馈及 Doctor 诊断问题，最终 5 项均通过冻结的外部验收。失败历史、运行产物和适用范围见 [实际能力验证报告](../OPEN_SOURCE_STATUS.md)；配置入口见 [Docker 使用说明](../../examples/sandbox/README.md)。

> **2026-10-31 Linux native 专项补测：** 原先跳过的 110 项已全部实跑通过；扩展 Linux 核心回归 195 项通过、0 跳过，另有 Docker 7 项和设计参考 13 项通过。修复了 native 运行时遗漏命名空间依赖的问题。见 [Linux native 报告与复现入口](../OPEN_SOURCE_STATUS.md)。

### v1.5.1 重点变化

- 文件副作用在写者栅栏内重查控制权限；取消等待执行中 Operation 和资源共同结算。
- 每个必需 Check 固定 `permitted_authorities`，低可信诊断不能充当全局必需判定。
- MCP 调用记录发送阶段；发送后 EOF、超时或损坏响应保持效果未知，不自动重放。
- 隐藏集一次性资格绑定不可变组件内容身份及血缘，事务内检查和预占，换 Candidate ID 无效。
- Meta-Eval 显式固定被测优化器，与 Coding 基线分离；诊断、生成、搜索调用记录实际 digest。
- G4 对完整候选 Release 运行固定 ABI、控制、验收、知识和隔离回归，而不只跑通用探针。
- successor 保留用户原目录和集成基线；patch 在任何文件效果前检查整个批次的路径并集。


独立的本地 Coding Agent：编写、运行、调试、验证和交付代码；在任务之外，通过受控实验改进自身策略。它不是项目学习助手。

本仓库依据 `docs/design/` 中的 v1.5 设计实现。**源设计文件保留为历史基线；实际实现、平台边界和已执行验证以本文、`docs/IMPLEMENTATION.md` 和 `reports/TEST_REPORT.md` 为准。**

## 已实现的主链路

```text
L0：用户目标 → 动态工具循环 → 隔离执行 → 外部验收 → 原子交付提交
                         ↓ 可信事件
L1：失败模式 → 定向归因 → 有界候选 → 开发/晋升/最终评测 → 完整 Release CAS
                         ↓ 实验记录
L2：冻结优化器 → L1 候选搜索 → L0 实际试跑 → 隐藏评分 → 新优化器审批
```

模型的“完成”仅触发验收，不代表 `VERIFIED`。权限、事实记录、裁判、复杂度硬预算和发布决策在可信 Host；生成的 Python 策略在发布前后都通过受限进程执行，不会 import 到 Host。

## 快速开始

### 运行环境

- Python 3.11+。原交付在 Linux / Python 3.13 验证；后续已补测 Windows Host + Docker Linux 执行，实际版本见能力验证报告。
- `native` 隔离后端需要 **Linux root、chroot/setuid 能力和 libseccomp**。建议只在独立测试容器或虚拟机内使用，不要为了试用而在日常主机随意运行 `sudo`。
- `docker` 后端要求已安装 Docker，并提供已经拉取或本地构建的 `repository@sha256:...` 镜像。Windows + Docker Desktop 已完成本轮任务验证；macOS 组合仍未实测。配置脚本及已测试镜像见 `examples/sandbox/`。
- `local` 是显式诊断模式，不能通过可信外部验证和自主发布门禁，不是安全沙箱。
- 网络默认关闭。任务依赖须提前装入执行镜像/原生运行时；模型调用由 Host 完成。不会通过未经批准的宿主机 `pip install` 替代隔离执行。

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[models,dev]'
repo-code --help
```

离线协议演示只需核心依赖：`python -m pip install -e .`。真实模型适配器在安装 `models` 额外依赖后可用。

### 1. 检查隔离环境

以下命令适用于上述 Linux 独立测试环境：

```bash
repo-code --state-root /tmp/repo-code-state --sandbox native doctor
```

Doctor 实际启动一个受限进程，检查低权限身份及网络限制。它还显示 SDK 是否安装，**不伪装为真实模型连通性检查**。首次运行会构建只读 Python 运行时；状态目录不能位于执行项目目录内部。

### 2. 无 API Key 的实际文件/进程演示

```bash
mkdir -p /tmp/repo-code-answer
repo-code --state-root /tmp/repo-code-state --sandbox native \
  --provider scripted --script examples/answer.script.json \
  run --workspace /tmp/repo-code-answer \
  --goal '创建 main.py，提供 answer()，返回 42；并验证。' \
  --contract examples/answer.contract.json --allow-write --allow-process
```

输出包含 `run_id`、工作副本路径、预算及实际交付记录。原始目录保持不变。`scripted` 是显式测试替身：它证明协议和执行链联通，不证明真实模型编码水平。

```bash
repo-code --state-root /tmp/repo-code-state --sandbox native report RUN_ID
repo-code --state-root /tmp/repo-code-state --sandbox native diff RUN_ID
repo-code --state-root /tmp/repo-code-state --sandbox native export RUN_ID /tmp/repo-code-export
# 只有原目录仍与创建任务时一致，才允许显式合入；否则拒绝覆盖用户的新修改。
repo-code --state-root /tmp/repo-code-state --sandbox native integrate RUN_ID --confirm
```

### 3. 真实模型

密钥只放在 Host 环境变量，不放入代码、Skill、命令参数、工作区或日志。

```bash
export OPENAI_API_KEY='在自己的终端设置真实密钥'
repo-code --state-root "$HOME/.repo-code" --sandbox docker \
  --image 'YOUR_PREBUILT_IMAGE@sha256:YOUR_ACTUAL_64_HEX_DIGEST' \
  --provider openai --model 'YOUR_AVAILABLE_MODEL_ID' \
  run --workspace /absolute/path/to/project \
  --goal '实现需要的功能并运行验证' \
  --contract /outside-project/acceptance.json --allow-write --allow-process --stream
```

镜像和模型为明确占位符，必须替换。Anthropic 使用 `--provider anthropic --model ...` 与 `ANTHROPIC_API_KEY`。兼容端点通过 `--base-url` 配置，也可通过 `--api-key-env` 指定环境变量名。

原交付未执行真实 API 测试。后续中转站补测使用本机环境变量中的密钥，新增的 `--reasoning-effort medium` 会发送给 OpenAI 兼容接口，并固定到 Run 配置以检查恢复一致性。协议接入结果见 [API 接入报告](../OPEN_SOURCE_STATUS.md)，后续隔离执行及交付结果见 [实际能力验证报告](../OPEN_SOURCE_STATUS.md)。自进化收益仍未验证。

以下命令适用于 PowerShell；默认不带 `--live` 时只检查本地配置，加上后实际访问 API 并产生费用。`--agent-diagnostic` 只在临时副本内读写文件，检查生成代码的 AST，不执行生成代码、不产生可信交付。

```powershell
.\.venv\Scripts\python.exe scripts/api_integration.py `
  --model gpt-6.1-sol --reasoning-effort medium `
  --base-url https://modelflare.dev/v1 `
  --report reports/api-integration/live-gpt-6.1-sol-medium.json `
  --live --agent-diagnostic
```

密钥默认读取 `OPENAI_API_KEY`，也可以通过 `--api-key-env` 指定名称；不传 `--base-url` 时读取 `OPENAI_BASE_URL`。正式 CLI、`scripts/live_smoke.py` 和 `scripts/live_benchmark.py` 同样支持 `--reasoning-effort medium`。

### 4. 空项目验收建立

```bash
repo-code --provider openai --model YOUR_MODEL contract draft \
  --goal '实现一个异步下载 CLI，支持超时与失败退出码' \
  --output /outside-project/contract.draft.json
# 操作者检查需求、外部接口、期望结果与覆盖矩阵后，才冻结：
repo-code contract freeze /outside-project/contract.draft.json \
  --reviewer local-operator --output /outside-project/contract.json
repo-code contract validate /outside-project/contract.json
```

`draft` 只是草案生成，不会自动批准。没有冻结契约可以开展探索性任务，但不能输出 `VERIFIED`。验收作者与实现调用分开；同一个模型生成的测试并不天然正确，必须审阅。

## 交互、审批与恢复

运行中的 CLI 会打开私有、鉴权的 loopback 控制入口。另一个 CLI 可向真正持有进程的 Host 发出取消，而不是猜测 PID：

```bash
repo-code --state-root STATE --sandbox native control RUN_ID cancel
repo-code --state-root STATE --sandbox native control RUN_ID pause
repo-code --state-root STATE --sandbox native control RUN_ID steer \
  --text '优先运行相关测试' --classification execution_only
```

新增/取消需求或更改验收范围，使用 `contract_change`，审阅新契约后创建 successor Run：

```bash
repo-code --state-root STATE --sandbox native control RUN_ID steer \
  --text '增加权限检查' --classification contract_change
repo-code --state-root STATE --sandbox native successor RUN_ID \
  --goal '实现新需求' --contract NEW_FROZEN_CONTRACT.json
```

Steering 接纳先于成功提交时，旧决定失效；成功提交先发生时，记录保留为历史交付。不会热替换正在运行的 TaskSpec、Contract、Release 或 KnowledgeSnapshot。

工具待审批会返回 `WAITING_APPROVAL`，可查 `operation`、明确批准或拒绝，再用相同模型配置 `resume`。移动/删除/MCP 调用默认逐次审批；`--allow-process` 授权的是隔离任务副本内的命令，不是宿主机 Shell。

```bash
repo-code --state-root STATE --sandbox native approve OPERATION_KEY
repo-code --state-root STATE --sandbox native --provider openai --model ORIGINAL_MODEL resume RUN_ID
repo-code --state-root STATE --sandbox native recover RUN_ID
repo-code --state-root STATE --sandbox native operation OPERATION_KEY --reconcile
```

重启后的未知进程/外部副作用不会盲目重放。文件操作可依据 Journal 前后状态对账；缺少进程归属证明时保持阻断。不要手工删除状态来“恢复”。

## Memory、Skills、MCP、子 Agent

- 原始会话与模型视图分离；折叠保留完整 ToolCall/ToolResult 组，详细历史可用 `artifact.read` 回取。
- 项目记忆默认按原工作区路径身份隔离；用户级记忆显式使用 `--scope user`。
- Run 固定 `knowledge_snapshot_digest + knowledge_index_digest`；新记忆只影响下一 Run。撤销是例外：旧快照也会被阻断。
- BM25 召回和作用域过滤已实现；未声称实现或验证 Dense/Cross-Encoder，它们在设计中是需消融后启用的可选项。
- `knowledge revoke` 是停止使用，不等同于销毁历史私有证据。有关删除请求、备份与历史副本见 SECURITY.md。
- 手动导入 Skill 需要操作者批准。自动提炼的 Skill 标记为 `origin=evolution`，不能通过 `knowledge approve` 绕过完整 Release 评测。
- 子 Agent 是预算受限的独立只读探索实例，不递归派生、不并行裸写工作区。并行写入/复杂 DAG 不作为本版已验收能力。
- MCP 为隔离的 **2025-06-18/2025-03-26 legacy stdio** 子集；不宣称当前全部 MCP 扩展兼容。原生后端已实测；远程 HTTP MCP 和 Docker stdio 适配未实现，连接时明确拒绝而非裸跑。

```bash
repo-code --state-root STATE knowledge draft --kind memory --title '项目约定' \
  --file notes.md --workspace /absolute/project --source user-confirmation
repo-code --state-root STATE knowledge approve KNOWLEDGE_ID --reviewer operator
repo-code --state-root STATE skill import path/to/SKILL.md --workspace /absolute/project
repo-code --state-root STATE skill export KNOWLEDGE_ID exported/SKILL.md
```

MCP 示例见 `examples/mcp/`，配置由操作者提供，工具仍经 Broker 审批。

## L1：问题驱动定向进化

```bash
repo-code --state-root STATE observe
repo-code --state-root STATE problems
repo-code --state-root STATE --provider openai --model YOUR_MODEL evolve diagnose PATTERN_ID
repo-code --state-root STATE suite-register dev.json --role dev --name dev-001
repo-code --state-root STATE --provider openai --model YOUR_MODEL \
  evolve search PATTERN_ID --dev DEV_COHORT --max-candidates 3
```

初版在线规则检测“相同状态下重复确定性失败”，记录症状而非宣称根因。诊断器只读取相关 DEV 证据并引用真实事件 ID，允许 `inconclusive/environment/trusted_safety`。不会轮流扫描所有组件。

候选支持 `memory_policy / context_policy / retry_policy / planning_policy / tools_schema / skills_index / delegation_policy`。可以替换配置或受限 Python `handle(payload)`；代码运行方式发布后不改变。不可修改权限、裁判、预算硬限、发布器。`REMOVE` 恢复固定组件槽位的可信默认实现，不删除安全控制。

注册独立 promotion、final cohort 后：

```bash
repo-code --state-root STATE --provider openai --model YOUR_MODEL evolve qualify CANDIDATE \
  --dev DEV_COHORT --promotion PROMOTION_COHORT --final FINAL_COHORT
repo-code --state-root STATE evolve audit CANDIDATE
repo-code --state-root STATE evolve approve CANDIDATE --reviewer operator
repo-code --state-root STATE evolve activate CANDIDATE
```

隐藏信息曝光沿候选/知识/优化器血缘传播；同题改名不能恢复独立性。G4b 最终评测失败会阻止发布。发布以整份 ReleaseManifest 做 CAS，拒绝未共同评测的组件拼接。候选活动和历史档案分离，`evolve retire` 不会删除被引用的恢复证据。

`enqueue PATTERN --dev COHORT` / `worker-once` 提供持久队列与一次处理。没有活跃编码任务时才领取；不自动启动常驻服务，也不宣称安装之后已在后台持续优化。

## L2：受控 Meta-Evolution

`evolve meta-qualify` 执行完整嵌套链路：冻结的 L2 实例提出 L1 搜索配置，L1 实际生成和评测 L0 候选，然后可信裁判在独立问题上评分。每个 arm/episode 使用独立临时状态；累计失败候选和所有模型调用成本。

L2 可以生成 `l1_optimizer / l2_optimizer` 的配置或受限算法。Builder 不取得隐藏题库；被测实例只取得本题允许公开的 Failure Pattern、DEV 证据与预算。自修改默认关闭，启用也不能绕过门禁：

```bash
repo-code --state-root STATE evolve enable-meta --reviewer operator --acknowledge-research-risk
```

本版验证的是嵌套流程、权限和预算契约；**没有通过真实模型实验证明元优化器变得更聪明**。

## 可选真实模型基准（不是本次已完成的验证）

本节指 `examples/live_benchmark_tasks.json` 的另一套五题数据；本轮实际完成的是 `examples/capability_tasks.json`，覆盖四类能力、共五个任务，结果见能力验证报告。两套数据不可混算。

新增五个标准库任务：单文件修复、空目录 CSV 处理、多文件购物车、异步分批和嵌套配置合并。
每题有冻结的外部行为检查及静态检查；完整结果、失败原因、实际 usage（接口返回时）和调用次数保存为 JSON。
没有凭据或 SDK 时记录 `BLOCKED`、退出码 2，不切换为 ScriptedProvider。

```bash
# 只检查环境，不访问模型服务
python scripts/live_benchmark.py --provider openai --model YOUR_MODEL \
  --sandbox native --state /tmp/repo-code-live-fresh \
  --report /tmp/repo-code-live-report.json --preflight-only

# 明确同意费用之后才真正执行；先在 Host 环境配置 OPENAI_API_KEY
python scripts/live_benchmark.py --provider openai --model YOUR_MODEL \
  --sandbox native --state /tmp/repo-code-live-fresh \
  --report /tmp/repo-code-live-report.json --i-understand-api-costs
```

Anthropic 使用 `--provider anthropic` 与对应环境变量。Docker 还需 `--image repository@sha256:...` 和已配置引擎。
这是小规模 smoke/regression 数据集，不是模型排名或 RSI 泛化收益基准；也不应反复优化后仍称为盲测。

## 一键离线闭环

```bash
repo-code --state-root /tmp/repo-code-demo-only --sandbox native demo \
  --output /tmp/repo-code-demo-output
```

演示执行实际文件与隔离程序、外部裁判、L1 候选搜索和完整发布 CAS。它使用有意设计缺陷的 ScriptedProvider，因此结果标为 `INTEGRATION_ONLY`。只在新演示状态目录显式允许该类证据激活；正常状态禁止将演示成绩当成生产提升依据。演示完成后会输出生成的程序和 JSON 报告。

## 测试与交付

```bash
python -m pip install -e '.[dev]'
# Linux 隔离测试环境；不满足条件会显式 skip，不等于验收通过。
python scripts/validate.py --suite native
# 另行验证原设计的13项参考模型测试：
python tests/design_reference.py
```

`scripts/validate.py` 拒绝把 skip 当作完整验收；用明确的 pytest-asyncio 插件集，避免宿主机自动加载无关插件。`--suite native` 明确选择 Linux 核心范围，Docker 后端 7 项使用 `--suite docker` 单独执行；默认 `all` 仍要求所选全部用例零跳过。最新 native 执行见 `reports/native-validation/REPORT.md`；原交付构建和解压复测结果保留在 `reports/TEST_REPORT.md`。

## 目录

```text
src/repo_code/    可信运行时、工具、隔离、验收、知识、L1/L2、CLI/SDK
examples/         契约、脚本模型、MCP 服务及 SDK 示例
scripts/          验证、构建和显式付费 live smoke
tests/          实现测试 + 独立设计参考模型
docs/design/    原始 v1.5 设计基线（不是实现状态声明）
docs/           实现映射、协议、运行与范围说明
reports/        实际测试日志、JUnit、构建/演示/解压检查
```

阅读 `SECURITY.md` 后再执行不熟悉的仓库。状态、备份和验收 oracle 都可能包含私有源码，不能公开上传。
