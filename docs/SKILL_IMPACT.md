# Skill Impact v0.1

Skill Impact 用于观察一条已冻结的 Skill 在 A/B 中是否真正进入请求、执行并获得独立验证。设计借鉴 NVIDIA SkillEvaluator 的成对比较和激活指标。A 使用 Base Release，B 使用只更改目标 Skill 的 Candidate Release。两组分别使用独立状态和工作区。

## 从已有 S2/S3 记录生成报告

先安装项目，再在仓库根目录运行。以下示例中的路径和候选 ID 应换成可信 Host 中的实际值。

```shell
python scripts/skill_impact_lite.py report --state /path/to/trusted-state --candidate-id CANDIDATE_ID --phase s2 --out reports/skill-impact
```

命令以只读方式打开 SQLite，产出 `results.csv`、`summary.json` 和 `report.md`。输出目录在 Host 状态目录之外。对同一份输入重复运行会得到相同内容。旧记录也可导出，报告保留原先记录的交付数，缺失的独立评分、目标 Skill 证据和完整 Usage 标为未知。报告不导出工作区路径、评分器定义、私钥和原始 Trace。

## 如何读结果

- `retrieved` 表示路由考虑了目标合同。`offered` 表示 Host 在一次实际请求中投递了目标。`bound` 对需要绑定的 Skill 检查本次 Run 的签名绑定，对无绑定槽的 Skill 为不适用。
- `applied` 依赖目标合同已有的回执和顺序正确的成功操作。`verified` 进一步要求回执中的独立验收证据。任务本身通过冻结评分器即能记录 `PASS`，目标 Skill 是否使用是另一个指标。
- 配对键是任务 ID 和尝试序号。报告检查固定成员、Release、模型、预算、环境、评分器、工作区基线及有效运行策略。预算内未结束记为 `NOT_COMPLETED`。未知结算、缺失评分或环境错误使该对不可比。只有完整配对实验才报告正式通过率差。
- 指标分别给出 Tool Calls、模型调用、输入、输出、缓存和耗时。缺少完整 Usage 时不计算对应差值。两组共同通过的子集只用于描述。`actual_cost` 需要账单证据。
- `INTEGRATION_ONLY` 是机制回归。`LOCAL_DIAGNOSTIC` 用于真实模型诊断。`INCONCLUSIVE` 表示激活或比较证据不足。`HARMFUL_OBSERVED` 同时保留正、负转换和误触发。正式收益结论要求完整比较及目标 Skill 的实际使用。

## 接入与回滚

`TrialRunner` 在运行结束后追加最小证据。S2/S3 返回 `impact_ref`，持久引用由 `skill_impact_stage` 关联到原阶段报告摘要。对资格签名和发布流程，原阶段报告仍是准入输入。新增派生记录存入现有 `objects` 表，无数据库迁移。回滚时撤回本迭代的代码、文档和分发哈希即可。已有派生记录可保留作审计。

离线验证用 `python -m pytest -q tests/test_skill_impact.py tests/test_skill_validation.py tests/test_procedure_promotion.py` 和 `python scripts/check_distribution.py`。Linux native 和 Docker 的隔离回归仍分别执行。真实模型测试通过独立的诊断驱动复用试验候选授权和现有评分器。
