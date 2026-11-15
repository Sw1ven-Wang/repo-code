# repo-code v1.5.1 运行、证据和发布协议

## 1. 信任域

| 主体 | 可见/可做 | 不可做 |
|---|---|---|
| 操作者 | 审阅契约、授权任务、管理私有状态、批准版本 | 不应把修改可信策略当作普通自进化结果 |
| Trusted Host/Broker | 凭据、操作事实、预算、外部判定、CAS、控制事件 | 不 import 生成的 Python 代码 |
| L0 Provider | 当前任务与允许的上下文/工具 Schema | 不持有数据库、主密钥和隐藏答案 |
| DUT/进化 Worker | 隔离副本、限定输入、JSON 输出 | 不直接访问 Host 私有目录、网络和发布状态 |
| Candidate Builder | DEV 证据、允许修改范围、有限反馈 | 不读取 promotion/final 的题库和逐题轨迹 |
| Frozen Eval Instance | 当前题的公开输入、必要 DEV 证据和预算 | 不读取别题、评分答案和后续候选材料 |

Host/Broker 属于同一可信域，可为同一进程的不同服务；安全隔离发生在不可信代码与该域之间，而非依赖文件夹命名。

## 2. 任务与控制

创建任务固定：TaskSpec、Contract、完整 Release、Knowledge Snapshot/Index、模型 adapter/model/endpoint、工作区基线、环境与预算。运行中不热换它们。

状态包括 `RUNNING / WAITING_APPROVAL / PAUSED / VERIFYING / NEEDS_RECONCILIATION / SUCCEEDED / FAILED / CANCELLED / SUPERSEDED / PARTIAL / ANSWERED`。具体可达路径以 `runtime.py` 为准。无冻结 Contract 的探索 Run 不获得 `VERIFIED`。

控制事件使用独立事件 ID 去重。被接纳时增加 control epoch，使旧审批/验证准备状态无法继续沿用。取消通过当前拥有进程的 Host 执行，不凭 PID 猜测另一个进程是否是目标。未知归属保持阻断；程序不接受调用者布尔值作为停止证明。

`execution_only` Steering 更新执行指导，不降低验收；`contract_change` 使旧 Run 停止，操作者提供新冻结 Contract 后生成 successor。继承的是受控工作副本，不继承批准范围或旧成功证据。

## 3. Broker IPC

loopback、单行 JSON、受限消息长度和短期 token；不用于公网。Token 绑定 Run 和 audience，控制与工具权限分开。工具调用必须先由 Host 记录真实 Provider 响应并注册，Worker 不能伪造一个全新的未注册工具调用。

语义请求数据结构：

```json
{
  "protocol_version": 1,
  "run_id": "run-...",
  "turn_id": "turn-...",
  "tool_call_uid": "host-assigned-call-...",
  "tool_name": "edit",
  "tool_spec_digest": "sha256-of-authorized-tool-spec",
  "arguments": {"path": "main.py", "old_text": "return 0", "new_text": "return 42"},
  "expected_state_digest": null,
  "approval_scope_digest": null
}
```

实际 envelope 和客户端见 `ipc.py`。OperationKey = SHA256(run_id, turn_id, tool_call_uid)，并绑定完整语义参数摘要。同键同参数返回原状态/结果；同键不同参数拒绝。响应丢失后使用 GET_OPERATION 查询原键。新的模型 ToolCall 并不是同一请求的网络重发，依然受到版本/重试/预算规则约束。

主要状态：`REGISTERED → WAITING_APPROVAL/AUTHORIZED → EXECUTING → SUCCEEDED/FAILED`；中断不明是 `UNCERTAIN`。未知副作用不能因重新连接就回到“未执行”。文件 Journal 可以通过前后快照核对；普通 Shell 或 MCP 不保证 exactly-once。

## 4. 验收定义与证据

必需检查全集不得为空；冻结流程拒绝未注册引用、未映射且非全局的必需检查、重复 ID、不受保护检查、空覆盖、错误 Subject，以及把同进程诊断注册为必需可信判定。

| Subject | 绑定 | 例子 |
|---|---|---|
| SOURCE | 本次源快照、检查定义、环境、Task/Contract/Release/Knowledge | 函数输出、HTTP/ASGI 行为、语法 |
| BUILD | 构建输入源快照 → 实际产物 manifest | 固定 build.py 产生归档 |
| ARTIFACT | 实际测试产物 manifest | 在 artifact-only 环境中加载产物 |

每个 Evidence 包含预期/实际执行项、跳过项、观察材料摘要、裁判版本、签名；全部必需检查必须通过，随后再检查每项需求覆盖。普通 `diagnostic` 的退出码不提升为外部可信证据。

函数和 ASGI 检查的 expected 保留在 Host。被测解释器可改变其自己的状态，但不能写 Host 的预期和 Evidence。它返回的 JSON/HTTP 协议是被判断的外部行为，不能据此证明所有内部语义或所有输入场景正确。

## 5. PreparedDecision 与 Delivery

检验结果只产生签名的 PreparedDecision。提交时重新核对：

```text
签名与固定契约/版本一致
AND required-check 全集有效
AND Run revision/control epoch/writer epoch 未变化
AND Run 当前可以提交
AND 无待处理 steering/cancel/approval/unknown effects
AND source/protected/knowledge 仍有效
```

成功 Delivery、Run 状态和 outbox 在同一事务写入；失败回滚，不可留下“已通知成功但无交付”的半状态。签名使用本地私有 Host 密钥，不声称是外部认证机构签名。存在文件系统与 DB 的边界，靠快照、锁和效果对账处理，不宣称跨文件 ACID。

## 6. 可进化组件 ABI

配置必须通过固定组件槽位校验；Python 实现采用：

```python
def handle(payload: dict) -> dict:
    return {"max_identical_failures": 2, "require_new_evidence": True}
```

它是独立受限进程，有限时间、内存、输出，结果再由 Host 验证。L1/L2 优化器可使用 `converse()`：返回受限 `model_request` 与可序列化 state，由 Host 代发模型请求，再把结果交回。最多有限轮，不能请求工具或权限升级。接口不是任意动态插件系统。

Policy 的“允许重试”“建议调用工具”只是行为建议，不能覆盖 Broker 的审批、路径、预算、签名或验收判断。

## 7. 发布协议

Release = 全组件摘要 + 可信 Worker 构建摘要 + 依赖摘要 + GatePolicy 摘要 + 构建记录 + base + generation。候选冻结的是完整组合，不存在两个分别验证的组件直接拼成新生产组合。

`G0` 结构/ABI/硬复杂度；`G1/G2` 开发集来源与回归；`G3` 独立晋升集；`G4` 候选完整 Release 绑定的兼容/控制/隔离回归及硬限；`G4b` 冻结版本最终独立核查；`G5` 人工批准并对 active manifest 整体 CAS。L2 还必须存在匹配的 `META` 证据与操作者显式开启。

基线变化必须重新产生对应组合的候选和报告，不能复用旧批准。每个 Run 固定原版本；新版本只影响新 Run。撤销权限/知识是安全控制，仍可阻断旧 Run。

脚本替身、合成演示被标记 `INTEGRATION_ONLY`，正常状态不能激活。离线 demo 在全新状态显式启用测试例外，并明确报告不代表真实模型质量。用户不得用该例外冒充生产改进成绩。

## 8. Meta-Eval

比较冻结的优化器实例，而非只比较它生成的一段 Prompt。每个优化问题/arm 使用全新状态；L2 选择有界 L1 搜索配置，L1 执行归因、候选与 DEV 试跑，最后外部评分在未暴露任务执行 L0。包括失败尝试的所有模型调用纳入总预算。

实例清理前保存私有 DB/Artifact 审计档案；保留原始 trace，使“trial ID”不变成悬空引用。开发材料可学习；隐藏反馈及其后代血缘不能重新取得同 cohort 独立性。最终题库不自动循环变成公开训练材料。

## 9. Review 修订后的补充不变量

- CANCEL 接纳撤销新 forward effect，终态等待所有活动/未知 Operation 与资源停止；`PENDING_DRAIN_*` 可随结果提交重新结算。CANCEL 优先于尚未生效的执行偏好，不会被旧 Steering 重新打开。
- 每个 Check 的 `permitted_authorities` 在冻结时验证。必需全集逐项检查 authority，不能依赖需求覆盖中的 `any` 替代。
- successor 的复制来源与集成目标分离。批量变更配额为 `历史 changed ∪ 整个批次 source/destination`，在 Journal 与文件效果之前验证。
- MCP 本地去重与远程效果未知是不同维度；`mcp_request.operation_id` 关联本地账本，发送后不确定失败不降级为 FAILED。
- `release_content_identity` 忽略可变显示/构建元数据，但固定所有实际组件和执行环境。候选、Release、内容身份及学习血缘共同限制隐藏集使用；预占与检查原子化。不能识别所有语义近似程序，仍需策展与可信数据流。
- G4 必须包含固定完整案例清单，并绑定候选 Release、组件、评测套件与环境；不允许拿仓库此前的 pytest 结果替代某一候选 G4。
- Meta-Eval 分别固定 Coding 初态和优化器，所有内层 L1 算法调用使用被测优化器。是否开启 L2 自修改仍需独立许可。
