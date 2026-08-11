# EVO-05.5f5x3d Remote Stable Member Finalization Executor

## 目标

在 [EVO-05.5f5x3c](EVO-05-5f5x3c-signed-remote-finalization-authorization.md) 的 signed、短期、
single-use Finalization Authorization 之上，完成一个真实跨边界 stable member binary finalization：

1. Control Plane 原子消费 Authorization，并签发绑定 exact Consumption 的 Execution Grant；
2. 目标安装验证 installer-owned Trust Policy、Authorization 与 Grant 两层独立签名；
3. 目标安装重读真实 `release-slots.db`、slot、manifest、Boot Receipt 和不可变二进制；
4. `ReleaseSlotStore` 以 expected-pointer CAS 写入 durable stable-member finalization；
5. 目标 installation key 使用独立 domain 签署 typed Finalization Result；
6. Control Plane 重新验证 current authority sources、current Population Credential 和结果签名后，记录 durable Receipt。

本切片实现可复制的 Base64 手动传输协议与本机目标执行入口。它不冒充 daemon/mTLS 自动传输，不聚合 Population
completion，也不授予配置/数据、deployment、rollback 或 promotion authority。

## 协议 artifact

### Execution Grant

`EvolutionStableRemoteFinalizationExecutionGrant` 内容寻址并冻结：

- exact Authorization ID/SHA 与 Authorization signature ID；
- exact durable Consumption Receipt ID/SHA；
- exact installation member、Credential、公钥与 release-root SHA；
- expected active pointer ID/SHA/generation；
- `finalize_stable_population_member` 唯一操作；
- `binary_only`、single execution 与 expected-pointer CAS 强制位；
- Authorization consumption time 至 Authorization expiry 的执行窗口；
- config/data、deployment、rollback 与 promotion authority 全部为 false。

Grant 使用 Rollout Control key 的独立
`naumi.release.stable-remote-finalization-execution-grant.v1` domain 签名，不能把 x3c Authorization signature
重放为 Execution Grant signature。`EvolutionStableRemoteFinalizationExecutionPackage` 同时携带原 Authorization envelope、
Consumption、Grant 与 Grant signature，并机械验证四者的 exact binding。

### Target Result

`EvolutionStableRemoteFinalizationResult` 包含：

- exact Grant、Authorization、Consumption 与 installation identity；
- 真实 `ReleaseStableMemberFinalization`，包括 authority、active pointer 和 writer 时间；
- target sources reread、两层控制面签名已验证与 expected-pointer CAS 已满足；
- binary stable-member finalization 为 true；
- config/data、deployment、rollback 与 promotion 执行为 false。

Result 使用 installation key 的独立
`naumi.release.stable-remote-finalization-result.v1` domain 签名。Remote Readiness Probe signature 无法跨 domain
冒充 Finalization Result，反向亦然。

## Control Plane prepare 与 single-use 协调

`prepare` 的顺序是：

1. 动态 inspect x3c Authorization；
2. 在 x3c authority DB 的 `BEGIN IMMEDIATE` transaction 中消费 exact nonce；
3. 在新的 Grant writer transaction 中重读 exact Authorization 与 Consumption；
4. 在同一 writer transaction 中重读 current rollout kill-switch generation；
5. 签署并持久化一个 Authorization-unique Execution Package。

同一 Authorization 的多 Service、多协程并发 prepare 收敛为一个 Grant。Control Plane 在 consume 后、Grant 写入前崩溃时，
重试会读取 durable Consumption 并继续形成 exact Grant，不会二次消费或另造 authority。

普通 `prepare` 不显示 bearer package；只有显式 `export <grant-id>` 在重新验证 current sources 与 Trust Policy 后返回 Base64。

## 目标端机械执行

`execute_stable_remote_finalization()` 在 writer 前完成：

- strict Base64/JSON、512 KiB 上限与 exact model round-trip；
- expected installation member、current time 与双层 Rollout Control signature；
- current local Population Credential、installation public key 和 release-root SHA；
- active pointer 前后两次一致读取；
- previous activation event 链；
- candidate 与 rollback slot ID/SHA、manifest、Boot Receipt；
- `resolve_booted_slot()` 对真实 bundle、immutable marker 与 binary digest 的复验；
- active pointer 的 previous pointer/slot projection；
- `ReleaseSlotStore.finalize_stable_member()` 内的 `BEGIN IMMEDIATE` 和 expected-pointer CAS。

Release Store authority schema 新增显式
`evolution_stable_remote_finalization_authorization` kind；本地 x2 authority 与远端 x3c authority 的 ID namespace 保持分离。
同一 authority 的 writer 重试返回同一 durable finalization；不同 authority 或 pointer 已变化时失败关闭。

## Control Plane ingest 与动态 inspect

`ingest` 只在 Execution Grant 仍有效时：

1. 重读 durable Package；
2. 动态 inspect x3c Authorization，要求 source、Probe、control 与 Trust Policy current，且 exact Consumption 存在；
3. 从 current Population Snapshot 解析 exact Credential；
4. 验证 Result 与 Grant、writer authority、pointer 和 release-root exact binding；
5. 验证 installation finalization signature domain 与 Ed25519 signature；
6. 在 Receipt writer transaction 中再次重读 Authorization、Consumption 与 kill-switch generation；
7. 幂等记录 `EvolutionStableRemoteFinalizationReceipt`。

Receipt 把 `stable_member_completion_fact` 与 `current_control_plane_authority` 分开。Control Plane 无法在 inspect 时直接读取远端
active pointer，因此 `remote_active_pointer_current_unverified=true` 是明确的不确定性；下一次 fresh Probe 才能更新该事实。
Receipt 不形成 Population completion 或 Promotion authority。

## 双通道与用户流程

- Agent Tool：`evolution_stable_remote_finalization`；
- `/evolution stable-remote-finalization prepare <authorization-id>`；
- `/evolution stable-remote-finalization export <grant-id>`；
- `/evolution stable-remote-finalization execute-local <package-base64>`；
- `/evolution stable-remote-finalization ingest <grant-id> <submission-base64>`；
- `/evolution stable-remote-finalization inspect <receipt-id>`。

CLI、Textual TUI 与 New UI 继续共用 Slash Router 和同一 Agent Tool Service。permissive/moderate/strict/bypass 均不触发
二次确认，lockdown 阻断；bypass 保持全权限直通语义。

## 验收标准

- [x] 真实两代 Release Store、current Population Credential、x3b Probe 与 x3c Authorization 作为输入；
- [x] 八路并发 prepare 收敛为一个 signed Execution Grant；
- [x] portable package strict round-trip，且不包含原始 workspace/release path；
- [x] 八路目标执行收敛为一个 durable release finalization；
- [x] active/previous pointer、candidate/rollback bytes 与 Boot Receipt 在 writer 前重验；
- [x] pointer 在 prepare 后变化时 writer 失败关闭且不形成 finalization；
- [x] installation key 以独立 domain 签署 typed Result；
- [x] 八路 ingest 收敛为一个 Receipt；
- [x] kill switch 在执行后、ingest 前变化时拒绝 Receipt；
- [x] tampered package/result 失败关闭；
- [x] Engine、public exports、Agent Tool、Slash、New UI forwarding 与权限同源；
- [x] 只运行相关小模块测试、Ruff、compile、YAML/public API 与 diff check；按用户要求不跑全量测试。

## 自我审视与下一步

本切片已执行真实目标 writer，不再把“已签发授权”当作“已完成 finalization”。仍未完成：

- daemon/mTLS installation transport、delivery acknowledgement、backoff、offline queue 与跨重启自动重投；
- Grant 临近过期、目标已写入但 installation key 暂时不可用时的超期 recovery/late ingest 协议；
- Control Plane inspect 对远端 active pointer 的新鲜重验；
- Population member Receipt aggregation、缺员/替换成员策略与 population-level Completion Authority；
- ARC-07.6 配置/数据迁移与独立 Promotion authority。

[EVO-05.5f5x3e Remote Finalization Delivery and Recovery](EVO-05-5f5x3e-remote-finalization-delivery-recovery.md)
已完成 transport-neutral durable outbox、安装端签名 ACK、claim/retry fencing、目标 journal 与只补签既有 writer fact 的受限
late-result recovery。下一步先接真实 authenticated transport worker，再进入 Population aggregation，不能直接跳到 fleet completion。
