# Memory v2：实验性迭代

对应 2026-11-01 的三份架构、技术和集成计划。新功能位于软件版本的 Unreleased 部分；旧 Run、旧知识快照和分发包不改写。实际灰度裁决见 `reports/memory-v2-2026-11-01/REPORT.md`。

## 配置与兼容

默认仍使用 v1 路径。显式配置才为新 Run 开启 v2：

```python
import json
from repo_code.service import RepoCode
profile = json.load(open("examples/memory/profile.json", encoding="utf-8"))
app = RepoCode(state_root, configured_provider, sandbox="docker",
               image=pinned_task_image, memory_profile=profile)
# app.new_task(...)；await app.execute(run_id)
# 使用结束时 app.close() 关闭离线编码 worker。
```

CLI 新增 `--memory-profile PATH`、`--thinking enabled|disabled`、`--max-output-tokens N`。恢复 v2 Run 必须提供原模型配置和原记忆 profile。同步 `ContextEngine.build()` 继续服务旧 Run；v2 使用异步 `MemoryService.prepare_context()`，不嵌套运行事件循环。

本轮 DeepSeek 实验使用 `deepseek-flash`，`thinking=disabled`、`max_tokens=4096`、`temperature=0`；CLI 对应 `--thinking disabled --max-output-tokens 4096 --temperature 0`。不要只复制模型名而遗漏推理参数。

启用 v2 将 Host SQLite `user_version` 升为 17，记忆 codec 为 2。旧二进制不能写入新状态；当前 Host 保留旧 codec 的读取与执行。升级要求旧 Run 已停止，旧 manifest 不变。回退时保留新状态审计，在兼容 Host 中为新 Run 选回 v1；不能直接用旧程序打开 schema 17。

`profile.json` 是未授权 protocol rebase 的完整检索初始配置；`deepseek-experimental.json` 仅对应本机已验证的模型参数与工具表。rebase 许可不自动扩展到其他模型、参数或工具表，完整请求过大且未获对应许可时停止。

## 真正的本地模型

```powershell
.venv/Scripts/python.exe scripts/setup_memory_models.py `
  --models-root E:/Coding/Project_Data/python_data/agent/repoCode/memory-models-v2 `
  --output examples/memory/encoder-profile.json --build --container-download
```

准备阶段联网下载；运行阶段禁止下载。使用固定 revision 的 multilingual-e5-small 和 mmarco-mMiniLMv2-L12-H384-v1，保存模型、tokenizer、全部文件 SHA-256 和 worker 镜像 digest。它们是方案候选模型的轻量实现选择，实际质量单独实测，不宣称等同 Qwen3 模型。

编码 worker 使用独立 Docker 容器：非 root、无网络、只读根目录、只读模型和脚本、无 Host 数据库/密钥/用户目录挂载，限制 CPU、内存和 PID，环境不传 API 凭据。摘要与主任务使用用户配置的同一 Provider；本地模型不用 API Key。

`hybrid_semantic_strict` 缺模型、哈希不符、维度不符或 worker 失败就报错。`lexical_only` 必须显式选择并单独报告，不伪造向量或把 BM25 报成语义检索。

## 上下文与历史

Host 状态来自 Operation、Contract、文件快照与实际验收，模型的完成声明不产生 VERIFIED。文件变化使旧检查显示 HISTORICAL/待重验。摘要对闭合前缀进行有界异步提取，保留 claim ID、原文引用、差分与两层导航。工具组不拆开；大结果外置，并行结果共享组预算。重要决定和开放问题优先，旧观察通过目录回取。

无权引用、字段非法、控制或撤销屏障变化时不接纳提案。部分无支持的解释可隔离，合法原文锚点与未验证解释分开；引用存在不证明语义蕴含正确。坏摘要不覆盖合法旧投影，放不下必需材料就明确停止。

入口为 `history.search/expand`、`memory.search/read/explain/propose`。作用域和 pin 从当前 Run 解析，不接受任意 snapshot/scope。历史读取不产生编辑授权；编辑仍需 Broker 的当前版本读取。

统一估计完整原生请求，包含别名、guidance、Skill、协议字段及输出保留。估算 tokenizer 不构成费用硬上限。主调用、摘要和巩固执行 SQLite 原子预算预占；未知 usage 保守保留并标记 `USAGE_UNKNOWN`，reasoning token 不重复加总。

## 检索与巩固

新 Run 固定知识与索引。当前意图随计划、文件和错误更新；先授权，再做词项/BM25、真实向量、RRF、真实 reranker，最后查依赖、文件版本、有效期与撤销。UNKNOWN 不冒充已适用；不适用项排除。缓存绑定权限、撤销、意图、版本、模型和请求预算，发送前复查。

终态、明确纠正和 Host 验证阶段触发持久化队列，每项目一个有效 worker 租约。部分检查通过而整体验收失败时，只记录 HISTORICAL 阶段观察，不产生交付成功；阶段记录不额外调用模型。终态和纠正的语义提取只产生候选。符合显式预授权的 Host episode，以及版本可核对的小型 JSON 文件观察，可成为后续 Run 的知识；默认不自动激活。重试不增加独立支持样本。冲突保留关系，旧作业不能在来源变化后覆盖新纠正。

行为规则不能走普通激活接口，仍需 Skill/完整 Release 评测和批准。PROMOTION/FINAL 轨迹不进入共享学习。摘要、知识版本、索引、缓存继承来源污染与撤销。L1/L2 不能修改授权、撤销或验收强制逻辑。

标准 Trial 固定 `eval:<cohort>` 的可信可见快照，排除个人记忆，并使用 `learning_enabled=false`：保留 v2 压缩与检索，关闭 `memory.propose`、终态和纠正写回。持续学习实验另行命名，不能混入标准评测。Trial 工具表变化后必须单独验证 rebase；原完整工具表的许可不会自动适用。

## 离线影子迁移

```powershell
.venv/Scripts/python.exe scripts/migrate_memory_v2.py `
  --source-state ORIGINAL_STOPPED_STATE --destination NEW_SHADOW_DIRECTORY `
  --profile examples/memory/profile.json --reviewer local-operator
```

源 Host 须停止，目标目录须全新。创建备份与影子状态，历史 manifest/pin 不变。旧条目标记 `legacy_unverified`，路径 scope 保留，不按同名目录或 remote 自动合并项目。

恢复 v2 备份会建立撤销同步屏障。Host API `MemoryRepository.sync_revocations(current_authority_store, reviewer)` 要求独立当前账本和匹配签名身份；缺少当前账本时不开放读取。迁移工具仅在明确指定当前源状态与 reviewer 后同步。即时访问阻断与后续物理清理分开，备份不宣称即时擦除。

## 复核

```powershell
.venv/Scripts/python.exe -m pytest -p pytest_asyncio.plugin tests/test_memory_v2_contracts.py --basetemp=.venv/pytest-memory-review
.venv/Scripts/python.exe scripts/run_native_tests.py --output NEW_REPORT_DIRECTORY
```

真实实验脚本为 `evaluate_memory_v2.py`、`memory_task_trials.py`、`memory_transfer_trials.py`，要求全新输出目录，会调用付费 API。Key 只从本机环境读取。失败、未知 usage 和辅助调用均纳入报告。小样本工程证据不提供行业能力或统计显著性结论。
