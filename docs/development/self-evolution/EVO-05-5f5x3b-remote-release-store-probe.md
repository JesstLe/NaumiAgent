# EVO-05.5f5x3b Remote Release Store Fresh Probe

## 目标

在 [EVO-05.5f5x3a](EVO-05-5f5x3a-authenticated-remote-readiness-claim.md) 的 current authenticated
installation claim 之上，要求目标安装从自己的 Release Store 重新读取 active pointer、candidate slot、previous pointer、
rollback slot 与 Boot Receipt，并使用 [ARC-07.5c1](../architecture/ARC-07-5c1-installation-key-provisioning.md)
安装私钥对有界结果签名。Control Plane 只在 durable challenge、x3a claim、Population Credential、签名与 freshness 全部
仍有效时，授予短期 source-runtime revalidation 和 binary rollback readiness authority。

本切片不下发远程执行命令，不修改 Release Store，不签发 rollout authorization，不执行 finalization，也不授予配置/数据
回滚、stable rollout、remote execution 或 promotion authority。

## 协议与真实数据源

### Control Plane Challenge

`EvolutionStableRemoteReadinessProbeChallenge` 绑定：

- exact x3a Claim Receipt、Population Completion 与 signed Snapshot；
- exact installation member、Credential ID/SHA 和 public-key SHA；
- candidate version/target；
- x3a 已观察的 active/candidate/rollback pointer、slot、manifest 与 Boot Receipt identity；
- 32-byte nonce、60..300 秒有效期和最多 30 秒探测执行窗口；
- 所有 mutation、readiness、rollout 与 promotion authority 初始均为 false。

Challenge 由 Control Plane 单飞持久化，同一 x3a claim 只对应一个 immutable challenge。Base64 只是手动传输编码，
不能作为 bearer authorization；Control Plane ingest 只接受自身 durable store 中 exact challenge 的返回。

### Remote Executor

`execute_stable_remote_readiness_probe()` 在目标安装本地执行以下机械步骤：

1. 要求 `ReleaseSlotStore` 与 `ReleaseInstallationKeyService` 属于同一 canonical release root；
2. 精确匹配 challenge 与本机 current Population Credential；
3. 读取 active pointer 和前一 generation activation event；
4. 分别调用 `resolve_booted_slot()` 重验 candidate/rollback immutable slot bytes、manifest 与原始 Boot Receipt；
5. 再读一次 active pointer，检测探测期间的 TOCTOU 变化；
6. 对所有 challenge expectation 做 exact comparison；
7. 生成不含原始路径的 source-set SHA-256；
8. 使用固定 `naumi.release.stable-remote-readiness-probe.v1` domain 签名 result。

整个执行器不调用 install、activate、rollback 或任何 writer API，Result 固定声明
`release_store_mutation_performed=false`。

### Control Plane Receipt 与动态 authority

Ingest 重新验证 result/challenge exact binding、current x3a claim、current Population Snapshot/Credential 与 Ed25519
installation signature；同一 challenge 的相同并发结果幂等收敛，不同结果永久冲突。每次 inspect 继续动态重验以上来源及
`expires_at`，任一来源变化或过期都会撤销：

- `source_runtime_revalidation_authority`；
- `binary_rollback_readiness_authority`。

历史 Receipt 仍保留，但配置/数据回滚、stable rollout、remote execution 与 promotion authority 始终为 false。

## 双通道与 UI

- Agent Tool：`evolution_stable_remote_readiness_probe`；
- Prepare：`/evolution stable-remote-readiness-probe prepare <claim-id> [validity-seconds]`；
- 目标安装执行：`/evolution stable-remote-readiness-probe execute-local <challenge-base64>`；
- Control Plane ingest：`/evolution stable-remote-readiness-probe ingest <challenge-id> <submission-base64>`；
- Inspect：`/evolution stable-remote-readiness-probe inspect <receipt-id>`；
- CLI、Textual TUI 与 New UI 走同一个 Slash/Tool Service；
- permissive/moderate/strict/bypass 均无需二次确认，lockdown 阻断。

## 验收标准

- [x] 真实两代 installed slots、active/previous pointer 与 Boot Receipt 被重新读取和字节重验；
- [x] 真实 installation Ed25519 key 只在目标安装签名，Control Plane 只用 Population public credential 验证；
- [x] 探测前后 active pointer 不变，执行器没有 Release Store mutation authority；
- [x] active pointer 二次读取变化时以 TOCTOU 错误失败关闭；
- [x] 结构完整但无法由 current Population Credential 验证的伪造签名不写 Receipt；
- [x] 两个 Service、八路并发 ingest 收敛为一个 Receipt；
- [x] freshness 到期后动态撤销两个 readiness authority；
- [x] durable Receipt JSON 篡改后读取失败关闭；
- [x] Tool、Slash、New UI forwarding、Engine、权限、public exports 同源；
- [x] 仅运行相关 Python 小模块与 terminal-ui state 模块测试、Ruff、compile；未运行全量测试。

## 自我审视与下一步

本切片真实重验远端安装数据源，但 transport 仍是显式 Base64 搬运，不是自动 daemon dispatch；Challenge 也不授权任何
writer。[ARC-07.5c2](../architecture/ARC-07-5c2-rollout-control-signing-authority.md) 已补齐独立 rollout-control
Ed25519 signing root 与 installer-owned Trust Policy，避免把 Channel/Build/Population/Installation key 错当 writer authority。
[EVO-05.5f5x3c](EVO-05-5f5x3c-signed-remote-finalization-authorization.md) 已消费 x3b readiness 与独立
rollout-control trust root，签发 member-scoped、短期、signed、single-use、binary-only portable Authorization，并继续受
kill-switch fencing、expiry、Trust Policy 与重放保护约束。下一最小切片 x3d 必须在目标安装真实验证/消费 envelope、执行
expected-pointer writer-fenced CAS 并返回 installation-signed result。只有每个 Population member 都产生 current readiness、
受限 Authorization 和 Finalization Receipt，才可聚合 Population Stable Rollout Completion。配置/数据恢复继续等待
ARC-07.6；三平台 OS keyring 的真实发布 runner 验收仍是 ARC-07.5c1 的外部发布门禁。
