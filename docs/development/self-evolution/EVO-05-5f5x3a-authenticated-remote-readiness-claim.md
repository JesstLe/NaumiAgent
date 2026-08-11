# EVO-05.5f5x3a Authenticated Remote Readiness Claim

## 目标

为 [EVO-05.5f5x3](EVO-05-5f5x3-stable-rollout-member-finalization.md) 的跨安装成员路径建立第一个真实、
可验证的身份边界：Control Plane 针对 current Stable Population 中一个 installation member 签发短期 challenge，
远端安装使用 Population Credential 对其本地 active pointer、candidate slot 与 rollback material assertion 做
Ed25519 签名，Control Plane 验证并持久化 authenticated claim。

本切片不远程执行 finalization，不把签名 assertion 当作已重读远端 SQLite，也不授予 binary rollback readiness、
stable rollout、remote execution 或 promotion authority。

## 为什么先做 Claim 而不是 Population 聚合

5f5x3 的真实 fixture 包含两个 Population member，但本机 Deployment/Release Store 只属于其中一个成员。直接让本机
Service 为所有 member 读取 pointer 或签发 Authorization，会把一台机器的 release store 冒充整个 fleet。Population-level
finalization 必须等远端成员具有独立身份、fresh readiness、受限 authorization、执行和结果回传链路后再聚合。

## Artifact 协议

### Challenge

`EvolutionStableRemoteReadinessChallenge` 内容寻址并持久化：

- exact Population Completion ID/SHA；
- exact signed Population Snapshot ID/SHA；
- member、Credential ID/SHA 与 installation public-key SHA；
- 对应 Stable Intent、subject、candidate version/target；
- 32-byte nonce、60..900 秒有效期和带时区时钟；
- execution、rollout、promotion authority 固定为 false。

Challenge 不包含私钥、原始机器标识、用户标识或工作区文件内容。

### Signed Assertion

`EvolutionStableRemoteReadinessAssertion` 由 installation private key 签名，冻结：

- Challenge ID/SHA 与 nonce SHA；
- member、Stable Intent、candidate version/target；
- active pointer ID/SHA/generation；
- candidate slot ID/SHA 与 manifest SHA；
- rollback pointer、slot、Boot Receipt ID/SHA；
- `expected_pointer_cas_observed=true` 与 `binary_rollback_material_observed=true`；
- observed time。

Assertion 只接受 canonical JSON，限制为 64 KiB；签名必须是 exact 64-byte Ed25519 signature。

### Claim Receipt

Receipt 绑定完整 Challenge、Assertion、signature 和 recorded time。Store 使用 Challenge 单飞键，完全相同的并发回传
幂等收敛，不同 assertion 永久冲突。Receipt 明确：

- `authenticated_installation_claim=true`；
- `source_runtime_revalidated=false`；
- binary rollback readiness、stable rollout、remote execution、promotion authority 均为 false。

## 动态 authority

每次 inspect 都重新验证：

1. Stable Population Completion 仍是 current authority；
2. signed Population Snapshot 仍是 current/trusted；
3. member 与 Credential ID/SHA/public-key SHA 仍精确一致；
4. assertion signature 仍可由 Credential public key 验证；
5. Challenge 尚未过期。

任一条件失效只撤销 `authenticated_claim_authority`，不删除历史 Receipt，也不升级为 readiness。

## 双通道与 UI

- Agent Tool：`evolution_stable_remote_readiness_claim`；
- Challenge：`/evolution stable-remote-readiness challenge <completion-id> <member-id> [validity-seconds]`；
- Ingest：`/evolution stable-remote-readiness ingest <challenge-id> <assertion-base64> <signature-base64>`；
- Inspect：`/evolution stable-remote-readiness inspect <receipt-id>`；
- CLI、Textual TUI 与 New UI 均走共享 Slash/Tool Service；
- Moderate 与 Bypass 均无需二次确认，Lockdown 阻断。

## 验收标准

- [x] 真实 2-member Population 可为非本机 member 签发 exact challenge；
- [x] 使用该 member 的真实 fixture private key 对真实 release pointer/slot lineage 签名并验证；
- [x] 错误 installation identity/signature 失败关闭且不写 Receipt；
- [x] 八路、两个 Service 并发回传收敛为一个 Receipt；
- [x] Challenge 过期后动态撤销 authenticated claim authority；
- [x] durable Receipt JSON 篡改后读取失败关闭；
- [x] Tool、Slash、New UI forwarding、TUI fallback、权限与 public exports 同源；
- [x] 小模块 pytest、Node test、Ruff、compile、文档登记与 diff check 通过；未运行全量测试。

## 自我审视与下一步

本切片只证明“这份 assertion 由 current managed installation identity 签发”，没有证明远端 daemon 从它自己的
Release Store 重新读取了这些字段。[ARC-07.5c1](../architecture/ARC-07-5c1-installation-key-provisioning.md) 已先补齐显式
OS-keyring installation key 与固定 readiness-probe 签名域。下一切片必须继续加入 freshness/fencing、远端 daemon execution challenge 与可验证
source digest，再把成功结果适配为 remote `EvolutionStableRollbackReadiness` 等价物。只有本地/远端每个 member 都形成
current readiness、受限 Authorization、Finalization Receipt 后，才允许实现 Population Finalization Authority。
