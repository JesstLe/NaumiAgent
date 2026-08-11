# ARC-07.5c2 Rollout Control Signing Authority

## 目标

为跨安装 Stable Rollout writer authorization 建立独立于 Build、Channel Catalog、Population Registry 和 Installation
Identity 的第四条 Ed25519 信任边界。Control Plane 后续只能使用本签名根签署固定 domain 的逐 member authorization；目标安装
只接受 installer-owned `trusted-rollout-controls.json` 明确授权的 control-plane key。

本切片只建立密钥、签名和公开 Trust Policy primitives，不签发 x3c authorization、不传输命令、不执行 finalization，也不授予
rollout 或 promotion authority。

## 为什么不能复用现有签名根

- Build signer 只证明 archive/source provenance；
- Channel signer 只决定 channel/target 的更新发现；
- Population Registry signer 只注册 managed installation 并签署 Population Snapshot；
- Installation key 只证明目标安装身份与本机读取结果。

让上述任一离线或安装侧私钥签署在线 writer command，都会扩大泄露半径并混淆“谁发布”“谁注册”“谁执行”。因此
`ReleaseRolloutControlKeyService` 使用独立 keyring account、metadata、policy 和固定
`naumi.release.stable-rollout-authorization.v1` domain。

## 显式 provisioning 与秘密边界

- 默认 `AgentEngine` 只构造 Service，不访问 keyring；
- `/evolution rollout-control-key provision [control-plane-id] [key-generation]` 才生成 32-byte Ed25519 seed；
- seed 只写 OS keyring，公开 metadata 以 `0600`、临时文件、fsync、atomic replace 落盘；
- 跨进程文件锁保证并发首次初始化只生成一把 key；
- keyring 写成功而 metadata 写失败时，删除 exact metadata 并补偿删除 secret；
- `inspect` 只读取 public handle，不读取私钥；
- provision 不自动写入 `trusted-rollout-controls.json`，避免本机生成 key 后自我提升为 fleet authority；
- metadata 不含 seed、原始 release path、机器或用户标识。

同一 release root 已绑定不同 control-plane ID 或 generation 时拒绝覆盖；轮换必须由后续显式 rotation/retirement 协议完成。

## Installer-owned Trust Policy

`ReleaseRolloutControlTrustPolicyDocument` 内容寻址并冻结最多 64 个 key：

- exact control-plane ID、key ID/generation 与 public-key SHA；
- `active|revoked` state；
- 唯一有序 channel scope；
- `valid_from`、可选 `valid_until` 与 `revoked_at`。

同一 Control Plane generation 只能出现一把 key，key ID 也必须全局唯一，避免同 generation 多 public key 的歧义信任。

Loader 限制 512 KiB，只接受不允许 group/world 写入的普通文件，使用 `O_NOFOLLOW`、打开前后 inode/size/time 对账并拒绝
symlink 或读取期间的路径替换。验证时同时要求 exact signer identity、active state、channel scope、签名时刻窗口、payload
digest/size 与 Ed25519 signature。Trust Policy 撤销可在后续 x3c 每次动态 inspect 时即时撤权。

## 签名 artifact 边界

`ReleaseRolloutControlSignature` 绑定 fixed domain、signer、channel、payload SHA/bytes 与 `signed_at`。Artifact 本身固定：

- `private_key_exposed=false`；
- `rollout_authority=false`。

它只证明 trusted Control Plane 对 exact bytes 签名。[EVO-05.5f5x3c](../self-evolution/EVO-05-5f5x3c-signed-remote-finalization-authorization.md)
已继续验证 x3b readiness、Population Completion、kill-switch generation、nonce、expiry 与消费状态，形成短期、single-use、
member-scoped writer capability；远端 executor 仍必须在 writer 前重验与消费。

## 双通道入口

- Agent Tool：`evolution_rollout_control_key`；
- Provision：`/evolution rollout-control-key provision [control-plane-id] [key-generation]`；
- Inspect：`/evolution rollout-control-key inspect`；
- CLI、Textual TUI 和 New UI 使用共享 Slash/Tool；
- permissive/moderate/strict/bypass 无二次确认，lockdown 阻断；
- 默认信任策略路径：`<release-root>/trust/trusted-rollout-controls.json`。

## 验收标准

- [x] 8 个独立 Service 并发首次 provision 收敛为一个 key，seed factory 只调用一次；
- [x] 默认 Engine 和 public inspect 不读取 keyring；
- [x] metadata 不含 secret/原始路径，POSIX 权限不向 group/world 开放；
- [x] active trusted key + exact stable channel + exact payload 的真实 Ed25519 验证通过；
- [x] revoked key、错 channel、伪造 signature、同 ID/generation 的 public-key 替换、metadata 篡改、policy symlink 与
  group/world 可写 policy 失败关闭；
- [x] metadata 落盘后异常会补偿删除 metadata 与 keyring secret；
- [x] release public API、Engine、Tool、Slash、New UI forwarding 与权限同源；
- [x] 只运行 rollout-control 小模块、Tool/权限和 terminal-ui state 模块测试、Ruff、compile、YAML 与 diff check；未跑全量测试。

## 未完成边界与下一步

- key rotation/retirement、multi-signer quorum、HSM/TPM/KMS non-exportable backend 和三平台真实发布 runner 尚未完成；
- Trust Policy 由 installer/管理员独立分发，本切片不提供“生成即信任”捷径；
- 自动 daemon transport、mTLS/network identity、delivery retry 与离线队列尚未完成；
- `EVO-05.5f5x3c Signed Remote Finalization Authorization` 已交付 portable signed capability；
  `EVO-05.5f5x3d Remote Stable Member Finalization Executor` 已实现独立 domain 的 signed Execution Grant、目标侧 Trust
  Policy/Release Store 重验、expected-pointer CAS 与 installation-signed result。自动 transport 和超期恢复仍需 x3e 完成。
