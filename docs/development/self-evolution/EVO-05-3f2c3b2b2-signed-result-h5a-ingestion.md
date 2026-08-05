# EVO-05.3f2c3b2b2 Signed Result Manifest 与本地 H5a 摄取

## 目标

将 authenticated remote Worker 的执行结果从“Worker 自报成功”升级为可机械验证、可恢复、可追责的本地 H5a 证据。
Worker 必须用领取 Claim 时登记的 Ed25519 私钥签署完整 typed RED/GREEN result prefix；Control Plane 在写入现有 Harness
H5a Store 前重新计算全部内容摘要、身份和执行绑定。本切片不授予 cohort、comparison 或 promotion authority。

## 权威链

每份 manifest 同时绑定：

1. current 或已在 current 窗口完成 durable admission 的 EVO-05.3f2c3b2b1 execution authorization；
2. exact authorization/Claim/Identity/Worker incarnation/Contract/Source/Platform/Suite；
3. authorization 中唯一的 Run Grant digest；
4. Worker 报告的平台身份及其 content digest；
5. 连续的 sample window，每个 sample 固定 RED 后 GREEN；
6. Runtime Contract 派生的 deterministic sample seed；
7. 完整 `HarnessEvalSuiteResult` typed JSON、精确持久化字节数和 H5a SHA-256；
8. Worker Ed25519 signature。

`EvolutionRevalidationPlatformResultStore.record_manifest()` 在单独事务中重读 immutable authorization 和 supervisor-attested
Worker Identity，再次校验全部静态 binding 与 Worker signature。首次 admission 还由 Control Plane runtime key 对
manifest/authorization/server `admitted_at` 生成 HMAC attestation；过期恢复必须先验证该 durable admission。Service 验证不是 Store
可信性的唯一来源。

## 全量预验证与零污染

manifest 在 durable admission 前必须完整通过：

- Harness H5a 使用与 `HarnessStore.record_eval_result()` 完全相同的脱敏、canonical JSON、4 MiB 单结果上限和 digest 算法；
- 含有必须脱敏内容的远端 result 被拒绝，不能让签名内容在持久化时静默改变；
- platform/configuration/source baseline identity 由本地 immutable source pair 和当前 Profile 重建；
- suite、contract、case 顺序、runner、probe binding、metric/status、lifecycle digest 与 `run_scope=cohort` 全部重验；
- 每个 case 必须绑定 authorization 的 exact Run Grant digest；
- 任一 artifact 不合格时，manifest、H5a 和 pair receipt 均不落盘。

只有整份 manifest 预验证通过后才进入 durable admission，避免恶意但有效签名的 Worker 用坏 artifact 占住 result window。

## 可恢复摄取

摄取顺序为：

1. durable manifest admission；
2. 按 sample index、RED/GREEN 顺序幂等写入现有 Harness H5a Store；
3. 使用与本地执行器相同的 builder 生成 `EvolutionRevalidationAdversarialSampleReceipt`；
4. 在 State Store 事务中重读 pair receipt，并核对 RED/GREEN H5a digest；
5. 生成 content-addressed ingestion receipt。

若进程在 RED H5a 后中断，重试会读取同一 admitted manifest，幂等复验已写 RED、继续 GREEN 和 pair receipt。为避免合法结果因本地
写入耗时超过短期 authorization 而永久悬挂，只有“首次 admission”要求 authority current；已准入 exact manifest 即使 authorization
自然到期仍可完成本地摄取，但不会因此获得 cohort 或 promotion authority。Source/Profile 漂移仍失败关闭。

## Prefix 与权限边界

- 一个 manifest 可提交 1..100 个连续 sample pair，并从当前 accepted prefix 开始；
- 不连续、重复 window 或同一 window 不同内容全部拒绝；
- ingestion receipt 明确区分 `result_received`、`h5a_ingested`、`full_cohort_received`；
- 即使 `full_cohort_received=true`，`cohort_authority=false`；
- Matrix lane、comparison、decision、rollout 和 promotion 不读取 Worker 自报结论，只读取本地 H5a/pair authority。

## 当前可信边界

本切片验证 H5a 中每个 case 的 lifecycle digest，并由 exact Worker signature 覆盖完整 typed result，因此能够证明“该认证 Worker 对这些
content-addressed 执行事实负责”。它尚未把远端 ToolJob 的完整 admission→running→terminal receipt chain 复制到 Control Plane；需要独立审计
每次远端 ToolJob 状态转换时，后续 transport protocol 还必须携带完整 lifecycle chain，且网络层使用 mTLS 或等价 authenticated channel。

## 验收结果

- 真实 Ed25519 Worker key 签署 typed RED/GREEN manifest，本地生成 H5a 与 pair receipt；
- 注入 GREEN H5a 写入失败后只留下 admitted manifest 和 RED H5a，authorization 到期后可从 exact manifest 安全恢复；
- 8 路并发摄取只形成同一份 manifest、H5a、pair receipt 和 ingestion receipt；
- 并发请求即使具有不同微秒时钟，也统一使用首次 durable admission 时间生成同一回执；篡改 admission HMAC 后读取失败关闭；
- 外部私钥伪造 signature、错误 Run Grant、错误 seed/identity/configuration 均在远端 evidence 落盘前拒绝；
- ingestion receipt Store 事务重读 pair receipt，并逐项核对 RED/GREEN H5a digest；
- 新增 4 个 result ingestion 场景；与 execution authorization、local adversarial sample 合计 16 个聚焦测试通过；未运行全量测试。

## 下一切片

[EVO-05.3f2c3b2b3](EVO-05-3f2c3b2b3-remote-platform-completion.md) 已从完整的本地 H5a/pair prefix 机械生成
required-platform cohort receipt，收口 Authorization/Worker capacity，并以 durable completion 门禁推动 Matrix lane 完成。下一步进入
EVO-05.4 immutable staged rollout plan 与 local canary executor。

整个真实自进化闭环仍需继续完成 staged rollout executor、运行监控、自动 rollback executor、Outcome 回注和 EVO-06 opportunity discovery；
任何文档或 UI 不得把“result 已摄取”宣称为“自进化已完成”。
