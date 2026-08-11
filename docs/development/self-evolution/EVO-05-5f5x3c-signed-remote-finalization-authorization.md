# EVO-05.5f5x3c Signed Remote Finalization Authorization

## 目标

在 [EVO-05.5f5x3b](EVO-05-5f5x3b-remote-release-store-probe.md) 的 current、installation-signed fresh
Release Store Probe 之上，由 [ARC-07.5c2](../architecture/ARC-07-5c2-rollout-control-signing-authority.md)
独立 Rollout Control key 签发逐 installation member、短期、single-use、binary-only 的 portable Finalization
Authorization。

本切片只建立可传输 writer capability、动态撤权和 durable consumption 协议；不发送网络命令、不修改目标 Release Store、
不执行 pointer CAS、不形成 Finalization Receipt，也不聚合 Population rollout completion。

## Authority 输入与隔离

签发前必须同时动态读取：

1. x3b Probe Receipt 及其 current View；
2. x3a authenticated claim、current Population Snapshot/Credential 和 installation signature（由 x3b inspect 递归验证）；
3. fresh active/candidate/previous/rollback pointer、slot、manifest 与 Boot Receipt identity；
4. current rollout kill-switch generation，且状态必须为 `active`；
5. installer-owned `trusted-rollout-controls.json`；
6. 显式 provision 的独立 Rollout Control private key。

Build、Channel Catalog、Population Registry 与 Installation private key 均不能签发此 writer capability。Control Plane 的
workspace 只以 SHA-256 进入 artifact，portable envelope 不包含原始工作区或 release path。

## Exact Authorization

`EvolutionStableRemoteFinalizationAuthorization` 内容寻址并冻结：

- exact Probe/Claim/Population Completion/Snapshot/Credential identity；
- exact installation member、installation public-key SHA 与 release-root SHA；
- candidate version/target、active pointer、candidate slot/manifest/Boot Receipt；
- rollback pointer、slot 与 Boot Receipt；
- rollout-control sequence/event ID/SHA；
- attempt chain 与 previous authorization；
- 32-byte start nonce、60..300 秒 TTL，并受 Probe 剩余窗口上限约束；
- 唯一 operation `finalize_stable_population_member`；
- `binary_only`、`single_use_required=true`；
- config/data、deployment、rollback 与 promotion authority 全部为 false。

Artifact 使用 canonical JSON 计算 source-set SHA、authorization SHA 和 ID。`ReleaseRolloutControlSignature` 使用固定
`naumi.release.stable-rollout-authorization.v1` domain 绑定 exact artifact bytes、`stable` channel、signer 和 signed time。

## Durable issue 与 single-use consumption

Authorization Store 与 Probe、Population 和 rollout-control evidence 共用 authority DB：

- `BEGIN IMMEDIATE` 内重读 exact Probe durable source 与 current kill-switch generation；
- 同一 Probe + 相同 source-set 的并发签发幂等返回同一未过期 Authorization；
- 过期或已消费后才允许形成下一 attempt，并绑定 previous ID/SHA；
- consumption 以 Authorization ID 唯一，exact nonce 重试幂等；
- consumption writer transaction 再次重读 exact Probe 与 kill-switch generation，阻断 inspect/consume 间的 pause 竞态；
- 错 nonce 或二次不同消费稳定拒绝；
- renderer 与普通 inspect 永不显示 nonce，仅显式 `export` 返回 portable envelope。

当前 consumption 是 Control Plane authority ledger。下一远端 executor 必须在真实 writer 前通过受认证 transport 完成/确认该
消费，并设计跨网络崩溃恢复；不能仅凭目标机内存标记声称 single-use。

## Portable target verification

`verify_stable_remote_finalization_authorization()` 允许目标侧对 envelope 做不依赖 Control Plane 私钥的机械验证：

- strict Base64/JSON 与 512 KiB 上限；
- exact installation member；
- Authorization issued/signature/current/expiry 时间顺序；
- 目标 installer-owned Trust Policy 中 exact full signer identity；
- current active key、channel scope、validity window、revocation state；
- exact payload digest/bytes 与真实 Ed25519 signature。

该函数证明 capability 真实且属于目标 member，但不替代目标 Release Store 的最终重读、expected pointer CAS、远端消费确认或
结果回传。

## 双通道与 UI

- Agent Tool：`evolution_stable_remote_finalization_authorization`；
- 签发：`/evolution stable-remote-finalization-authorization issue <probe-receipt-id> [validity-seconds]`；
- 检查：`/evolution stable-remote-finalization-authorization inspect <authorization-id>`；
- 显式导出：`/evolution stable-remote-finalization-authorization export <authorization-id>`；
- CLI、Textual TUI 与 New UI 使用共享 Slash/Tool Service；
- permissive/moderate/strict/bypass 均无二次确认，lockdown 阻断；
- `export` 只允许 current Authorization，普通 inspect 不泄露 bearer nonce。

## 验收标准

- [x] 真实两代 Release Store、Population Credential、installation signature 与 x3b Probe 全链作为输入；
- [x] 两个 Service、八路并发签发收敛为一个 signed Authorization；
- [x] envelope 不含原始 workspace/release path，且 strict round-trip；
- [x] installer-owned Trust Policy 对真实 Ed25519 signature 验证通过；
- [x] wrong member、伪造 signature、revoked Trust Policy、paused kill switch、expiry 与 durable JSON 篡改失败关闭；
- [x] 八路 exact consumption 收敛为一个 Receipt，错 nonce replay 拒绝；
- [x] Engine、public export、Agent Tool、Slash、New UI forwarding 与权限同源；
- [x] 只运行本模块、注册/权限与 terminal-ui state 小模块测试、Ruff、compile、YAML 和 diff check；未跑全量测试。

## 自我审视与下一步

本切片不再是 unsigned/local-only SQLite 票据，但还不是远端完成闭环：

- 自动 daemon dispatch、mTLS/installation network identity、delivery retry 与 offline queue 未实现；
- 目标安装尚未在 writer 前重读自身 Trust Policy、active pointer 与 immutable slot bytes；
- Control Plane consumption 与目标 Release Store writer 无法共享事务，需要 crash-recoverable journal；
- 尚无 installation-signed Finalization Result ingest，也没有 Population aggregation。

下一最小切片是 **EVO-05.5f5x3d Remote Stable Member Finalization Executor**：目标侧验证 portable envelope，完成远端
single-use 协调，在真实 `release-slots.db` 内执行 expected-pointer fenced CAS，并用 installation key 签署结果；Control Plane
只在重新验证 current sources 与结果签名后记录 Finalization Receipt。
