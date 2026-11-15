# 隔离 stdio MCP 示例

`server.py` 提供 sum(a, b)，仅使用 Python 标准库。配置中的 root 必须替换为实际绝对路径；服务根会被复制进受限执行域，不直接运行在 Host。

在 `run` 命令前加 `--mcp-config /outside-project/mcp.json`，模型先 `mcp.discover` 再 `mcp.call`。调用默认需要明确审批。实现覆盖 legacy 2025-06-18/2025-03-26 的 initialize、tools/list（含分页）与 tools/call；不是远程 HTTP MCP 实现。

本版 CLI 自动装配仅在首次 `run` 建立连接。跨进程继续 MCP 任务时须通过 SDK 重新装配相同服务，或者启动新任务；不应把失去响应的外部调用当作未执行重新发送。普通文件/进程审批恢复不受此限制。
