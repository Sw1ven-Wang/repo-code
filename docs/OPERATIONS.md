# 操作手册

## 环境与安装

先读 README 和 SECURITY。核心只依赖 Pydantic，开发/测试环境安装 `.[dev]`；付费接口还需 `.[models]`。生产环境应自行锁定依赖，并使用已经审计的隔离镜像。本交付包含 wheel，不包含第三方依赖、模型、API Key 或完整 native runtime kit。

推荐在一次性 Linux VM/容器里进行 native 演示。`doctor` 必须实际通过低权限及网络限制探针。Docker 后端需要真实 digest 且镜像预先存在；不会自动 pull 或失败后退回 local。Host 与 native `/usr/bin/python3` 应同一 Python 次版本，避免复制的原生扩展 ABI 不兼容。

## 一次开发任务

1. 使用空目录或干净的已有项目，并选择 state-root 在其外部。
2. 建立目标、外部接口、冻结 Contract；不能把模型自动生成的草案当作已审阅标准。
3. 运行 `run`。工具写入的是私有任务工作副本；初次移除/移动和 MCP 写入需要批准。
4. 通过 `report / diff / processes / operation` 查看实际状态。`PARTIAL` 不是成功。
5. `export` 生成冻结交付副本；明确确认后才 `integrate`。原目录变化会阻止合入，不自动覆盖。

无 Contract 可以做探索，但不能得到受验收保护的交付。复杂真实需求应有多个测试及人工条件；一个返回42的样例不是通用编程质量基准。

## 中断与恢复

正常取消走活跃 Host 的鉴权控制通道，由拥有进程的执行器停止进程树。Host 硬退出后，先 `recover`，再查询具体 Operation；只对具有确定性前后状态的文件操作执行 `operation KEY --reconcile`。

遇到 `UNCERTAIN / NEEDS_RECONCILIATION` 不要删除 DB、伪造“进程已停止”或重发新键。部分文件批次根据 Journal 回滚；有外部修改时保留现场。旧进程生命周期无法证明、MCP 外部效果不明时，需要操作者调查，系统不提供 force-bypass。

审批后 `resume` 必须使用原模型配置。新增/取消需求创建 successor 并审阅新契约；执行偏好 Steering 不改变验收。

## 进化的显式操作

Observer 使用持久水位读取 DEV 事件。失败模式是待验证假设，不是模型自动得到的因果证明。 `enqueue / worker-once` 是有界一次处理，不会自动安装守护进程。

开发、晋升和最终 suite 要独立策展。复制同题并换 ID 不会获得独立性；相似答案泄漏仍需人工审计。新候选先 qualify，然后 approve、activate；不得修改隐藏评测器或硬门禁以刷高分。

L2 自修改默认关闭。第一次启用前应检查嵌套模型调用总开销、隐藏数据可见性及实际收益；离线脚本演示不是性能证明。

## 备份与保留

停止执行后：

```bash
repo-code --state-root STATE --sandbox native maintenance inspect
repo-code --state-root STATE --sandbox native maintenance backup --destination NEW_PRIVATE_BACKUP
repo-code --state-root STATE --sandbox native maintenance prune
# 真正清理需要显式 --apply，且使用全新备份目录
repo-code --state-root STATE --sandbox native maintenance prune --apply --destination NEW_PRIVATE_BACKUP
repo-code --state-root NEW_EMPTY_STATE maintenance restore --source PRIVATE_BACKUP
```

备份包含私有源码、知识及本地签名密钥。它不是公开审计导出；不能附在 Issue 或发送给不可信服务。恢复只写全新目录，校验清单和 DB；半恢复标志会阻止继续运行。

知识 revoke 停止使用，历史快照仍为审计保留。涉及个人信息销毁时，操作者必须同时处理备份和历史副本；本版没有通用跨备份擦除接口。

## 常见错误

| 错误 | 应对 |
|---|---|
| `SDK_MISSING / API_KEY_MISSING` | 安装 models extras；在 Host 环境配置，不写进任务文件 |
| `PINNED_IMAGE_REQUIRED / SANDBOX_UNAVAILABLE` | 配置受支持隔离环境；不要用 local 假装通过正式验收 |
| `CONTRACT_NOT_FROZEN / EMPTY_ACCEPTANCE` | 审阅并冻结非空契约；检查 required 与覆盖矩阵 |
| `WAITING_APPROVAL` | 查看原 Operation，批准或拒绝，再原配置恢复 |
| `RUN_MODEL_CONFIGURATION_CHANGED` | 用原 adapter/model/endpoint 恢复；新模型需要新 Run |
| `STALE_CONTROL / REVERIFY_REQUIRED` | 控制或源状态变化，重新准备验证；不覆盖旧签名 |
| `ORIGIN_CHANGED` | 导出 Diff 后人工合并，不自动清理用户目录 |
| `UNCERTAIN / RESOURCE_LIFETIME_UNKNOWN` | 保留状态，对账实际效果和进程归属 |
| `HOLDOUT_CONTAMINATED / HOLDOUT_TASK_REUSED / HOLDOUT_ALREADY_USED` | 使用真正独立的新样本；不能改 ID 重复刷分 |
| `RELEASE_CAS_CONFLICT` | 重新在当前完整组合上评测，不独立拼接组件 |

精确错误码以对应模块为准。本表是常见错误类别，不是全部枚举。

## v1.5.1 升级与实时状态

升级前见 [升级说明](UPGRADING_1_5_1.md)，推荐全新状态目录。取消可能暂时显示 `CONTROL_PENDING`：它已经撤销后续执行许可，但仍在等待已有 Operation 结束或补偿；不要把该状态当作“已完全停止”。未知效果进入 `NEEDS_RECONCILIATION`。

新必需 Check 可以冻结 `permitted_authorities`；低可信诊断不能成为全局必需检查。旧的 G4 通用探针报告不能用于新版本激活，必须重新运行候选绑定回归。
