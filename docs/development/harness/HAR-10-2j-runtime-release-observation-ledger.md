# HAR-10.2j Runtime Release Observation Ledger

## 目标

在 HAR-10.2i 的 exact release identity binding 上，为 managed New UI/TUI runtime 的每次 durable heartbeat
建立 append-only、content-addressed observation sample。该账本让后续 EVO-05 observation window 能读取真实连续样本，
但本切片不定义持续时间、最小样本数、exposure 或 Stage Completion 规则。

## Artifact 与权威边界

`HarnessRuntimeReleaseObservation` 冻结：

- exact binding/runtime identity ID 与 SHA-256；
- workspace、surface、subject、instance、epoch；
- heartbeat sequence、phase、observed time、timeout 与有限 detail code；
- chain origin、前序 sample digest 与自身 content identity。

每个 sample 固定 `heartbeat_observation_authority=true`，同时固定
`current_liveness_authority=false`、`rollout_observation_window_authority=false`。历史 sample 只证明 producer 当时成功提交了
什么；当前健康仍须用明确 `assessed_at` 机械计算，rollout window 必须由后续独立 policy 聚合。

## 同事务写入与连续性

Harness Store schema v26 新增 observation ledger。managed runtime 的以下写入都在原 heartbeat SQLite
`BEGIN IMMEDIATE` 事务内完成：

1. binding + sequence 1 `starting` + origin sample；
2. running、pulse、waiting、resume、draining、stopped/failed heartbeat + 对应 sample；
3. 完全相同 heartbeat replay 必须找到 exact sample，否则失败关闭；
4. sample 插入失败会回滚 heartbeat latest snapshot。

Producer 增加独立 record lock，并只在 Store 成功后推进本地 sequence。这样并发 pulse/状态切换不会争用同一 sequence，
Store 故障后的下一次写入也不会形成不可恢复缺口。

## 升级、读取与清理

- 新 managed runtime 的 chain 从 `startup/sequence 1` 开始；
- schema v25 已存在 binding + latest heartbeat 时，不伪造缺失历史。首次 v26 写入冻结
  `legacy_snapshot/current sequence` origin，之后才严格连续；
- `list_runtime_release_observations()` 每页最多 500 项，复验 binding 索引、sample artifact、连续 sequence 和前向 hash；
- cursor 必须对应真实已验证 sample，不能任意跳入不存在的位置；
- runtime retention 删除 terminal/offline heartbeat 时，同事务删除整条 observation ledger 和 binding，避免孤儿历史。

## 验收结果

- managed 生命周期产生 starting/running/pulse/waiting/resume/draining/stopped 共 8 个 exact sample；
- 多页读取保持 binding/runtime identity 一致和 hash continuity；
- observation insert 故障同时回滚 heartbeat 与 producer sequence，修复后可从同 sequence 重试；
- durable payload 篡改读取失败关闭；
- v25 latest heartbeat 升级后只生成诚实的 legacy baseline；
- concurrent producer state writes 串行分配 sequence；
- retention 同事务删除 observation、binding 与 heartbeat；
- 相关小模块测试、ruff、compile/public import 与 diff check 通过；未运行全量测试。

## 当前边界与下一步

[EVO-05.5f4a](../self-evolution/EVO-05-5f4a-opt-in-liveness-window.md) 已定义独立、content-addressed liveness-window
policy/receipt；[EVO-05.5f4b](../self-evolution/EVO-05-5f4b-durable-opt-in-liveness-assessment.md) 已继续完成 durable
分页取证与动态失效。真实 completed-run evidence 仍必须独立建立，分页结果和 liveness window 都不得直接冒充 Stage
Completion Evidence。
