# EVO-05.3f2b2a Fresh Runtime Evaluation Contract

## 1. 目标

在真正执行 Fresh Evaluation 前，重新冻结可执行 metric runner 与 adversarial probe authority。普通 Harness checks
不能替代定量 metric 或对抗探针；缺 fixture、runner、probe coverage 或预算时必须机械阻断。

## 2. Metric runner 绑定

`EvolutionRevalidationRuntimeContractBuilder` 从 Revalidation Validation Plan 的 metric pairs 重新调用受信任 Registry：

- `self_review_static` 只接受已实现 finding code、decrease、非负整数 target，绑定 `self_review_static@1`、固定 fixture
  digest 与单样本 timeout；
- `harness_replay` 在没有精确 replay fixture 时返回 `replay_fixture_required`；
- `feedback_recurrence` 在没有 observation-window runner 时返回明确 blocker；
- Profile check timeout 与 metric timeout 按 `2 × requested samples`（RED + GREEN）汇总，超过原 Experiment
  budget 时阻断；不能用单 phase 预算冒充完整 paired evaluation 预算。

## 3. Adversarial probe 绑定

Contract 使用版本化 Probe Registry，按每个改动路径重新选择 boundary、concurrency、security、recovery、
cross-platform 与 reward-hacking requirements，并只接受 Revalidation Validation Plan 中保留的 current Profile checks。
每个 requirement 必须有且只有一个声明对应 `adversarial_probes` capability 的匹配 check；缺失和歧义分别形成 blocker。

为此，05.3f2a Validation Plan 现在保留完整 selected current checks，而 required lint/compile/unit/contract coverage 仍要求
唯一 check。否则专用 adversarial checks 会在 runtime contract 之前被错误丢弃。

## 4. Authority 与双通道

SQLite Contract 以 Validation Plan 为唯一键，事务内验证 plan ID/digest，重复签发幂等。动态 inspect 以原始时间重建并
重读 current Plan；Profile、source 或 registry 漂移使 Contract stale。

- Agent Tool：`evolution_revalidation_runtime_contract(validation_plan_id=...)`
- 手动入口：`/evolution revalidation-runtime-contract <validation-plan-id>`

中风险、无需重复确认；Contract 不运行命令、不授予 Shell、Git、promotion 或发布权限。

## 5. 验收标准

- 受支持 metric 绑定真实 runner/version/fixture/timeout；未支持 verifier 明确 blocked；
- 每个 probe requirement 唯一映射 current check capability；缺失/歧义阻断；
- 完整 RED/GREEN 最坏 timeout 不超过原 Experiment budget；
- Store 不可变、幂等且拒绝缺失的 Validation Plan；
- Plan stale 后 Contract 动态 stale；
- Tool、Slash、权限、engine composition 与模块导出完整；
- 只运行相关聚焦测试，不运行全量测试。

## 6. 后续边界

本 Contract 本身仍是不执行命令的 authority。
[EVO-05.3f2b2b1](EVO-05-3f2b2b1-fresh-interventional-sample.md) 已让单个 Interventional RED/GREEN pair 消费
Contract 与共享 Source Pair，真实运行 Profile checks 和 ready metric runners；
[EVO-05.3f2b2b2](EVO-05-3f2b2b2-fresh-interventional-cohort.md) 已形成完整连续 cohort，EVO-05.3f2b2b3 仍需生成
paired comparison，EVO-05.3f2b2c 再接 Adversarial probes 与平台 lane。它们完成前不能签发新的
attribution 或 Final Evaluation。
