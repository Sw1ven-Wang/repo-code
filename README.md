# repo-code

一个独立运行的 Python Coding Agent：通过可信 Host 管理模型工具调用、隔离执行、上下文与记忆，并用独立验收约束交付和经验复用。

**MIT · Python 3.11+ · 实验性项目 · 包版本 1.5.1 / 源码快照 2026-11-15**

## 结构

```text
L0  任务 → 模型/工具循环 → 隔离工作区 → 独立验收 → 交付
                ↓ 执行证据
L1  Episode → 经验/Procedure Draft → 独立验证 → 会话复用或 Release 候选
                ↓ 冻结的实验和反馈
L2  元优化器 → 有界 L1 搜索 → 独立质量与发布门禁
```

- 工具包括文件检索、版本绑定编辑、批量 patch、进程和受控 MCP；副作用由 Host 管理。
- 上下文预算、历史归档与可选 Memory v2 profile；**默认保持 Memory v1**。
- 可选 `--context-compaction live-experimental`：用确定性历史卡片压缩模型视图，保留原始证据；**默认仍为 legacy**。见 [行为与回滚](docs/LIVE_COMPACT.md) 和 [验证边界](docs/LIVE_COMPACT_REVIEW.md)。
- L1 经验有来源、适用条件、证据引用、撤销与应用回执；草案保存不等于激活。
- `skill.bind` 将抽象能力绑定到当前任务已经读取的文件与验证命令。
- 会话级经验复用与永久 Release 分开；保留独立验证和 G3/G4/G4b 门禁。
- Provider 可选 `scripted`、`openai`、`deepseek`、`anthropic`、`chatgpt-plan`，不按结果自动换模型。

这些是工程机制，**不代表自进化已经获得性能提升**。最近一轮26题留出验证中，经验指导实际送达为零，不能用试跑波动宣称 L1 收益。见 [实验边界与聚合结果](docs/EVALUATION.md)。

## 安装

```bash
git clone https://github.com/Sw1ven-Wang/repo-code.git
cd repo-code
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -e '.[models,dev]'
repo-code --help
```

`scripted` 只需核心依赖。使用可选订阅 Provider 时另装 `.[chatgpt]`，按 [Provider 配置](docs/CHATGPT_PROVIDER.md) 独立授权；不会读取或分发别人的登录凭据。该文档保留历史兼容说明，当前 CLI 支持的参数以 `--help` 和实际错误码为准。

本项目提供 Agent/harness 源码，不包含 GPT、DeepSeek 等基础模型权重，也不附带账号、Token 或本机测试数据。少量教学示例和原有单元测试 fixture 保留；SWE-bench 等真实评测数据、Gold Patch、隐藏测试和实验集合须独立准备。

## 隔离与运行

`docker` 需要 Docker 及预先构建、用 digest 固定的任务镜像；配置见 [sandbox 示例](examples/sandbox/README.md)。`native` 需要 Linux root、chroot/setuid 和 libseccomp，适用于独立测试环境。`local` 是诊断模式，不等于可信隔离。模型调用在 Host 发生，凭据必须放在工作区外。

```bash
# 将镜像占位符替换为自己已经构建/拉取的镜像摘要。
repo-code --state-root /absolute/path/to/private-state \
  --sandbox docker --image 'YOUR_IMAGE@sha256:YOUR_64_HEX_DIGEST' \
  --provider scripted --script examples/answer.script.json \
  run --workspace /absolute/path/to/task \
  --goal '创建 main.py，提供 answer() 返回42，并验证' \
  --contract examples/answer.contract.json --allow-write --allow-process
```

原目录与 state-root 必须分离。模型说“完成”只会触发检查，不等于已经通过。真实模型使用显式的 Provider/模型 ID；API Key 从 Host 环境变量读取，不写入工作区或源码。用户需自行确认账号能力、可用模型与接口兼容性。

## 开发和验证

```bash
python -m pip install -e '.[dev,chatgpt]'
python scripts/check_distribution.py
# 普通开发检查；Windows 的 native 隔离项会跳过，不能作为完整验收。
python -m pytest -p pytest_asyncio.plugin tests -q
# 独立 Linux 环境内的完整 native 验证：
sudo .venv/bin/python scripts/validate.py --suite native --timeout-seconds 1800
```

Linux CI 保留完整 native 路径；Docker 和真实模型验证分别记录。发布快照的本地检查见 [开源状态](docs/OPEN_SOURCE_STATUS.md) 和 [贡献说明](CONTRIBUTING.md)。跳过、未执行和通过分别报告，不混算。

## 文档与接入

- [实现与平台边界](docs/IMPLEMENTATION.md) · [可信协议](docs/PROTOCOL.md) · [操作手册](docs/OPERATIONS.md)
- [Memory v2](docs/MEMORY_V2.md) · [Memory v2.1](docs/MEMORY_V21.md)
- [跨任务累计](docs/multitask-learning.md) · [原生学习衔接](docs/L1-COLLECTION-LEARNING-LINK.md)
- [Procedure 晋升](docs/procedure-promotion.md) · [通用能力绑定](docs/generic-capabilities.md) · [会话经验](docs/session-experience.md)
- [Skill Impact v0.1](docs/SKILL_IMPACT.md)
- [SWE-bench Adapter](integrations/swebench/README.md) · [Harbor 接入](integrations/harbor/README.md)
- [持续任务控制器](integrations/continuous_memory/README.md) · [历史设计](docs/design/README.md)
- [历史 README](docs/history/README-v1.5.1.md) · [安全边界](SECURITY.md)

评测接入代码不是附带官方任务环境的一键基准；启动真实实验前仍须准备数据、固定任务镜像、验证评分器和信息隔离。历史文档的本机报告路径仅作历史说明，原始运行材料未公开。

## License

[MIT](LICENSE)。使用的模型、SDK、数据集与第三方镜像仍分别遵循其自身许可与访问条件。
