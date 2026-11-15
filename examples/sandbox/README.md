# Docker 执行镜像

2026-10-31 已在 Windows + Docker Desktop Linux 引擎构建并测试验证镜像；最新实际任务结果见 [能力验证报告](../../docs/OPEN_SOURCE_STATUS.md)。

`Dockerfile` 保留为通用模板；`Dockerfile.validation` 是本次实测配置。`validation-config.json` 保存实际仓库摘要和模型参数，不含密钥。当前镜像仅安装 Python、标准库及列明的 pytest/FastAPI/httpx 等测试依赖；不包含 Node、Java、浏览器或项目业务依赖。

```powershell
# 检查现有固定镜像及实际沙箱启动，不访问模型
./scripts/setup_docker_sandbox.ps1

# 日常 CLI：自动带上该镜像及 gpt-6.1-sol / medium 配置
./scripts/repo-code-docker.ps1 --help

# 需要重建时执行；构建时联网安装依赖，任务运行期禁网
./scripts/setup_docker_sandbox.ps1 -Rebuild

# 按配置运行真实任务，会产生 API 调用费用；每次使用新目录
.\.venv\Scripts\python.exe scripts/capability_benchmark.py `
  --state .repo-code/capability-retest `
  --output reports/capability-validation/live-retest --live
```

模型由 Windows Host 调用，默认读取 `OPENAI_API_KEY`。运行期容器无 Docker socket、模型凭据或宿主项目目录挂载，只挂载该次执行的工作区副本；使用非 root、禁网、只读根文件系统及 CPU/内存/PID 限额。写入结果经现有 Broker/Journal 合入任务副本。

每次 `process.run` 启动独立容器。工作区文件变更会回收至任务副本，`/tmp` 不跨调用保留，Shell 的 `cd` 和环境变量也不跨调用保留。需要跨调用使用的产物应写在允许的工作区路径内；依赖同一 `/tmp` 文件的步骤应放在同一条进程调用中。任务的相对路径从 `/workspace` 开始。

使用含 Python 与目标 Shell 的实际 digest-pinned 基镜像。依赖在可信构建过程安装，最终按实际镜像摘要执行。本机 Docker Desktop 镜像存储能为本地构建提供仓库摘要，无需上传到公共 registry；若其他环境没有 RepoDigests，需要使用操作者控制的 registry。仅本地 image ID 不等同于项目要求的仓库 digest 引用。完整实际依赖版本保存在 `reports/capability-validation/image-installed.txt`；重建时传递依赖可能变化，因此始终记录新摘要。

Linux native 后端复用系统 Python 并复制测试包；Host 与 `/usr/bin/python3` 应具有同一 Python ABI。优先在独立 VM/容器里使用，不建议对未知代码在日常主机授予 root 启动权限。

Linux native 的一次性测试主机使用 `Dockerfile.native-validation` 和 `native-validation-config.json`。运行 `python scripts/run_native_tests.py --output reports/native-validation/retest-new` 会在外层 Linux 容器中执行真正的 native 后端，并在结束后清理容器；不会传入 API 凭据或 Docker socket。当前已完成原 110 项及完整 Linux 核心回归，见 [专项报告](../../docs/OPEN_SOURCE_STATUS.md)。
