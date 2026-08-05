# EVO-05.3f2a Revalidation Validation Plan Authority

## 1. 目标

在 target 前进并完成三方重放、Harness revalidation 和旧证据失效后，为完整 Fresh Evaluation 签发新的、不可执行的
Validation Plan authority。它纠正旧 `EvolutionValidationPlan` 仍绑定原 Candidate baseline/Lease 的问题：

- RED 必须是 Fresh Plan 指定的 current target revision/tree；
- GREEN 必须是同一 current target 加 EVO-05.3f1 immutable overlay；
- seed、预算、metrics、suite、样本数和平台集合必须来自旧 durable authority，不允许模型重新解释；
- checks 必须来自当前 Harness Profile，并逐文件、逐 check kind 唯一覆盖；
- 本 authority 只允许后续评测执行器消费，不运行命令，也不授予 promotion。

## 2. Authority 输入链

`EvolutionRevalidationValidationPlanService` 每次签发或检查时重读：

1. current Fresh Evaluation Plan 与其动态 eligibility；
2. immutable Evaluation Source 及全部 content-addressed blobs；
3. Promotion Package Input、Experiment Contract Authority；
4. 旧 Final Evaluation Receipt 的 suite、requested samples、Candidate 与平台集合；
5. current Harness Profile 生成的 revalidation check plan；
6. Source Snapshot target revision 上每个变更路径的真实 Git blob 和 executable mode。

任何 ID/digest、Candidate revision、文件 scope、GREEN digest、target operation、mode、Profile 或 check coverage 不一致都
fail closed。计划的 `created_at` 参与摘要；动态 inspect 使用原始时间复算，避免未漂移 authority 被时钟误判 stale。

## 3. RED/GREEN 与检查覆盖

每个文件记录 `modify|create`、RED digest、GREEN digest、executable 和语言感知的 required checks。原 patch 的操作语义
不能因 current target 文件出现或消失而被静默改变。每个 required check kind 必须由 current Profile 中恰好一个匹配的
`change` check 提供；缺失和歧义都阻断。

RED/GREEN 共享 `revision/tree`，只有 GREEN 挂载 immutable overlay。由此排除“旧 baseline 对新 patch”或 RED/GREEN
环境不同造成的虚假增益。

## 4. 持久化与双通道

SQLite 以 immutable Source Snapshot 为唯一键保存 content-addressed Plan，并在同一事务中确认 Source authority 已存在且
digest 相同。重复签发幂等，不允许覆盖冲突。

- Agent Tool：`evolution_revalidation_validation_plan(source_snapshot_id=...)`
- 手动入口：`/evolution revalidation-validation-plan <immutable-source-id>`

两者共用同一 Service；权限为中风险、无需重复确认。bypass 保持全权限，但不会把非执行 authority 提升为 Git、发布或
promotion authority。

## 5. 验收标准

- current Git target 同时成为 RED 与 GREEN baseline；GREEN 仅叠加 immutable blobs；
- seed、预算、metric pairs、suite、requested samples 与 required platforms 可追溯到 durable authority；
- create/modify、executable mode、Candidate revision 或 Final Evaluation 投影漂移均阻断；
- 所有文件的 required check kinds 有且只有一个 current Profile check 覆盖；
- SQLite 记录不可变、幂等，Source 不存在或 digest 错误时拒绝写入；
- Tool、Slash、权限和 engine composition 完整；
- 聚焦测试使用真实临时 Git 工作区、immutable source capture 与 SQLite；不运行全量测试。

## 6. 当前边界与后续

本切片没有执行 RED/GREEN、没有生成 cohort/lane/comparison/attribution，也没有签发新的 Final Evaluation。下一最小切片
EVO-05.3f2b 必须让 Interventional 与 Adversarial GREEN source adapter 消费本 Plan 和 immutable blobs，同时让 RED
从同一 current target 运行；之后才能复用现有 aggregation/final-evaluation kernel 形成新的完整证据链。重新审批、灰度、
监控和回滚必须继续等待新 Final Evaluation 与决策 authority。
