# Continuous Memory 评测修正

本目录提供新版本任务包生成器、逐轮微验收、隔离判题和分项报告，不改动既有 Mini10 题包、原始 trace 或分数。它不是新增 Memory 架构。

`upgrade_pack.py --source 原任务包目录 --output 新目录` 要求输出目录不存在。新版本将接口提前到 T1，保留后续纠正规则；修正 Queue T29 召回 / T30 迁移错位；澄清 dry-run、公开 CLI 语法并补强终态验收。该版本应另建实验，不与 V1 混算。

每次同 Run 的无工具 final 到达 `WAITING_USER` 后，可信 controller 使用 `grade_stage.grade(task, received_turn, ...)` 在独立、断网、只读候选和只读 grader 的容器评分。T37 前只检查已收到的阶段需求，不将完整终态用例用于模型失败归因。隐藏判题结果只保留在可信侧；不要把 grader 文件、未来轮次或隐藏期望灌入 Solver。

`controller.summarize()` 将已发轮次、模型交付轮次、当轮验收、终态验收和召回分列，未到达探针是 N/A。未见终态全部要求的候选即使作终态诊断，也只能放在 diagnostic 栏。

`require_calibration()` 要求完整参考通过、完整 Usage、预算内完成、首探针到达和逐轮无未来信息执行的可信校准记录。现有 7/5 项全绿的简化参考不满足这项要求。旧参考在新增分页、list_jobs、audit_events、状态和 CLI 检查中应暴露缺口。没有预算正对照时可以报告受限预算下的开发进展，不能据此发布长程 Memory 准确率或声称 37 轮可达。
