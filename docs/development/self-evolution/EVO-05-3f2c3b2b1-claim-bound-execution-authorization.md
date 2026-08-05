# EVO-05.3f2c3b2b1 Claim-Bound Remote Execution Authorization

## 目标

在 authenticated Worker claim 与远端 RED/GREEN 执行之间建立不可绕过的权限边界。该切片复用现有 HAR-08 父权限、Runtime
lease 和可撤销 Run Delegation Grant，形成 exact claim-bound execution authorization；不接受 Worker 自报的 `execution_started`，
不接收 result artifact，不写 H5a，也不改变 Matrix lane。

## 权威来源

Authorization 必须同时满足：

1. EVO-05.3f2c3b1 durable Dispatch 仍绑定 current Runtime Contract、Source Snapshot 和 exact Worker reservation；
2. EVO-05.3f2c3b2a Claim lease 当前有效，Ed25519 Identity、Worker incarnation 与 claim receipt generation 未漂移；
3. 父 `PermissionDecisionReceipt` 真实存在、允许执行、绑定 run id，并明确允许委托 `bash_run`；
4. `HarnessStore.acquire_run_lease()` 为父 run 获取 exact owner/epoch；
5. `RunDelegationGrantAuthority.issue()` 在同一 lease epoch 上签发只含 `bash_run` 的短期 Grant；
6. control plane runtime key 对最终 canonical authorization 生成 HMAC-SHA256 durable attestation。

父权限的新鲜度、来源类型、非传递性、工具子集与 lease fencing 继续由既有 Run Grant Authority 机械执行，不在 Evolution 中复制一套
弱化规则。bypass 仍可作为合法父权限来源，但不能跳过 lease、Grant、Claim 或 Worker fencing。

## 精确执行范围

每份 authorization 不可变绑定：

- Contract / Validation Plan / Source Snapshot / Dispatch / Job / Reservation；
- exact Claim receipt、lease epoch、Worker Identity 与 Worker contract；
- platform、suite、seed 和完整连续 sample indexes `0..N-1`；
- 固定 RED/GREEN phases、probe registry 和每个 probe-check binding digest；
- 唯一允许工具 `bash_run`；
- Worker 单 case output 上限，以及 `min(case_limit * 2 * samples, 1 GiB)` 聚合 result 上限；
- parent permission digest、完整 Run Grant envelope、Runtime lease owner/epoch；
- Claim、Run Grant、Runtime lease 三者中最早的 expiry。

Authorization 的 `result_submission_authorized=true` 只表示后续 ingestion service 可以在当前窗口接收一个待验证 manifest；它不表示
result 已收到或可信。

## Generation 与恢复

Authorization 是 append-only hash chain：

- 首次为 `authorization_sequence=1`；
- Claim 续租后，旧 generation 先以 `claim_lease_superseded` 撤销 Grant、释放 Runtime lease并持久化 revocation，再签发下一
  generation；
- authorization 自身到期但 Claim 仍 current 时，以 `authorization_superseded` 收口旧 generation 后可重新签发；
- 每个新 generation 绑定前一 authorization digest；
- `operator_cancelled` 等显式撤销不能被自动重签绕过。

并发签发使用 deterministic lease owner/Grant idempotency key 和 SQLite sequence constraint；跨进程重复只能得到相同 generation，不能
产生两个有效 Grant。

## Saga 与失败关闭

签发顺序为 Runtime lease → Run Grant → durable authorization。若后续步骤失败，Service 补偿撤销 Grant 并释放 lease。Store 在
`BEGIN IMMEDIATE` 中重读 current Contract、Dispatch 和 latest Claim receipt，校验 generation chain，且只有旧 generation 已有 durable
revocation 时才允许写入下一代。

动态 inspect 重新验证 control-plane attestation、Claim、Contract、父权限、Run Grant 及 Runtime lease。任何一项失败都返回
`stale/expired/revoked`，不会保留 execution 或 result-submission authority。

## 状态边界

本切片只产生执行授权：

- `execution_authorized=true` 仅存在于 current view；
- persisted artifact 仍为 `execution_started=false`；
- `result_received=false`；
- `cohort_authority=false`；
- `comparison_authority=false`；
- `promotion_authority=false`。

HMAC durable attestation 不替代网络侧 server authentication。未来 HTTP/queue transport 必须使用 mTLS 或等价的 authenticated channel，
且不得向 Worker 导出 control-plane runtime secret。

## 验收结果

- 8 路并发只形成一份 authorization、一条 active Run Grant 和一个 Runtime lease；
- 缺父权限、过期 Claim、错误 control-plane key 均失败关闭；
- Claim renewal 自动收口旧 generation 并形成 hash-chained 新 generation；
- authorization 到期后可在 current Claim 下形成下一 generation；
- operator revoke 不可自动绕过；
- revoke 真实撤销 Grant、释放 Runtime lease并持久化不可变 receipt；
- 模拟 authorization Store 写入失败后，Grant 与 lease 均完成补偿清理；
- 9 个 authorization 场景以及 Claim/Dispatch 共 19 个聚焦测试通过；Ruff/import 通过，未运行全量测试。

## 下一切片

EVO-05.3f2c3b2b2 将在 current authorization 下接收 Worker Ed25519-signed、内容寻址且有大小上限的 result manifest；逐项验证
platform/source/configuration/sample/phase、H5a typed JSON、lifecycle/Grant provenance 与 artifact digest，在本地不可变 Store 成功摄取后
才设置 `result_received=true`。任何部分结果只能作为可恢复前缀，不能形成 cohort 或 Matrix completed authority。
