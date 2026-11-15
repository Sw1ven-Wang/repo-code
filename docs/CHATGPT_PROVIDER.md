# 可选 Provider 与 ChatGPT 订阅接入

JSON 辅助请求会在 Responses `input` 中加入明确的 JSON 输出指令，并计入请求预算。不能只依赖顶层 `instructions` 或历史文本偶然包含 `json`。普通非 JSON 请求保持原格式。API 的 `invalid_request_error` 会安全标记为 `CHATGPT_INVALID_REQUEST`，保留已知参数名称，不记录原始错误正文或凭据。

这是 **repo-code 自己的 Agent + ChatGPT 订阅推理**。没有启动 Codex CLI/App Server，也没有借用它们的 Agent 循环。读写、进程、Memory、上下文压缩和验收仍由 repo-code Host 管理。

## 安装与选择

在项目根目录执行；以下 PowerShell 示例将状态与凭据放在 E 盘。

```powershell
.\.venv\Scripts\python.exe -m pip install --cache-dir E:\Coding\pip-cache -e '.[models,chatgpt]'
```

每次命令显式选择 `--provider`。没有根据结果或错误自动换 Provider 的行为，默认仍是离线的 `scripted`。

| Provider | 认证与接口 | 模型选择 |
| --- | --- | --- |
| `chatgpt-plan` | repo-code 专属 OAuth；官方 Responses；使用获准的订阅用量 | `models` 返回的 `slug` |
| `deepseek` | 默认 `DEEPSEEK_API_KEY_REPO`；`https://api.deepseek.com` | 显式 `--model` |
| `openai` | 默认 `OPENAI_API_KEY`；原有 Chat Completions，可设置 `--base-url` / `--api-key-env` | 显式 `--model` |
| `anthropic` | 默认 `ANTHROPIC_API_KEY`；原有 Messages | 显式 `--model` |
| `scripted` | 离线响应脚本 | `--script`，不构成模型成绩 |

## 首次订阅授权

本机 Codex 登录与 repo-code 的授权相互独立。此功能不读取 `~/.codex/auth.json`。

```powershell
.\.venv\Scripts\python.exe -m repo_code.cli --auth-root E:\Coding\repo-code-auth\chatgpt-plan auth login
.\.venv\Scripts\python.exe -m repo_code.cli --auth-root E:\Coding\repo-code-auth\chatgpt-plan auth status
.\.venv\Scripts\python.exe -m repo_code.cli --provider chatgpt-plan --auth-root E:\Coding\repo-code-auth\chatgpt-plan models
```

登录会打开浏览器，使用 **Continue with ChatGPT**，检查应用名为 repo-code 并授权使用订阅。正常情况下回调使用 `127.0.0.1` 的空闲端口；可以用 `auth login --port 1455` 指定。无法自动打开浏览器时使用 `auth login --no-browser`，手动打开显示的登录链接。登录默认等待 180 秒，可用 `--timeout` 调整（最多 600 秒）。

账号/地区/工作区必须获准使用官方预览接口。登录成功、列出模型均不等于已完成实际推理。`auth status` 的 `live_inference_verified` 初始为 false；完成推理后记录最近验证的模型、response ID 和时间。这个历史回执不保证未来可用性；真实调用结果仍以 Run 事件和 Usage 为准。

Windows 的认证与订阅推理会使用当前用户已启用的系统 HTTP/HTTPS 代理；不修改系统设置。显式 `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY` 保留 HTTP 客户端原有规则。可用 `REPO_CODE_CHATGPT_PROXY=http://127.0.0.1:10808` 指定本 Provider 的代理，或设为 `direct` 关闭本功能的自动系统代理选择。代理地址不进入模型上下文或 Run manifest。

可以用 `REPO_CODE_AUTH_ROOT` 环境变量设置固定凭据位置；显式 `--auth-root` 优先。都没有设置时，默认是 `--state-root` 同级的 `.repo-code-auth`。不同账号使用不同 auth-root，避免覆盖已注册账号。

## 运行与切换

将 `ACCOUNT_MODEL_SLUG` 换为上一条 `models` 返回的模型 slug；将 workspace 和 image 换为自己的任务与固定摘要镜像。每个实验使用新的 state-root。

```powershell
.\.venv\Scripts\python.exe -m repo_code.cli `
  --state-root E:\Coding\repo-code-runs\chatgpt-001 `
  --auth-root E:\Coding\repo-code-auth\chatgpt-plan `
  --provider chatgpt-plan --model ACCOUNT_MODEL_SLUG --reasoning-effort medium `
  --sandbox docker --image 'YOUR_IMAGE@sha256:YOUR_DIGEST' `
  run --workspace E:\Coding\my-task --goal '修复缺陷并运行测试' --allow-write --allow-process
```

切回 DeepSeek 时使用新的 state-root 和 `--provider deepseek --model deepseek-flash`，并移除订阅专属选项；原有 `--provider openai --base-url https://api.deepseek.com --api-key-env DEEPSEEK_API_KEY_REPO` 同样保留。API Provider 使用各自的计费方式。

正在执行或恢复的 Run 不能更换 Provider、模型或推理设置；`model_config` 会固定 adapter、model、endpoint、reasoning_effort，以及订阅的 billing / wire_protocol。比较模型时应新建 Run，而不是把旧会话切到另一个 Provider。

## 边界与记录

- 官方订阅预览不接受 `temperature`、`max_output_tokens` 等参数。对 `chatgpt-plan` 指定 `--temperature`、`--max-output-tokens`、`--thinking`、`--base-url` 或 `--api-key-env` 会明确报错，避免静默忽略。订阅端输出无法通过该接口设置硬上限；repo-code 的调用次数、工具次数、耗时和本地上下文准入继续有效。
- `--memory-profile` 保持原入口。模型输出的原生 reasoning / function_call / message 完整保存在 Run 历史，并在后续请求中重放。加密推理也计入本地请求估算。不得未经回归验证就对新协议开启 Memory 的协议重建白名单。
- 请求固定 `store=false` / `stream=true`，仅使用 `https://api.openai.com/v1/responses`，不使用 backend-api 或 `previous_response_id`。工具放入 `repo_code` namespace；执行仍需 Host 校验和预算准入。
- 只在收到完整的 `response.completed` 后返回可执行工具意图。断流、失败、不完整响应、重复/越权工具名均不会执行。Usage 保留服务端原始 `input_tokens` / `output_tokens` 及明细；订阅实际金额无法按 API 单价推定。
- 额度/权限/协议错误原样保留机器错误码及安全诊断信息，不自动改走付费 API，不自动重试模型推理。
- 凭据独立于 Run、Memory、交付导出目录。Windows 用当前用户 DPAPI 加密；POSIX 使用 0700 目录 / 0600 文件。刷新有跨进程锁，原子替换轮换令牌。认证目录不得与 workspace 或 state-root 重叠，也不得挂载进任务容器。

退出会尝试撤销远端可续期会话，再清理本地令牌并保留客户端注册与 Host ID；若无法确认远端撤销，会明确提示。

```powershell
.\.venv\Scripts\python.exe -m repo_code.cli --auth-root E:\Coding\repo-code-auth\chatgpt-plan auth logout
```

官方依据（2026-11-03 核对）：[订阅接入概览](https://developers.openai.com/siwc/token-sharing-open-source)、[授权与注册](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)、[模型与推理](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)、[预览限制](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)、[凭据续期](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)。
