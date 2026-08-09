# ARC-07.5c Signed Managed-installation Population Snapshot

## 目标

为 percentage rollout 提供不可由本地客户端自行编造的 population denominator。客户端只接受受信 Population Registry
Ed25519 key 签发的安装凭证与完整 population snapshot；本模块不从本机路径、用户名或 machine ID 推导“全局安装数量”。

该切片是 EVO-05.5f5a 的最小 ARC 前置。它建立 assignment 输入 authority，但不执行分桶、部署、更新下载、流量扩大或 telemetry。

## Registry Trust Root

`ReleasePopulationTrustPolicyDocument` 独立于 build trust：

- registry/key ID、generation、Ed25519 public key 与摘要；
- active/revoked 状态、valid-from/until 与 revoked-at；
- canonical ordered keys、content-addressed policy ID/digest；
- credential 与 snapshot 必须在 exact trusted-key 时间窗内签发；
- key 不可信、已撤销、过期或 signature 无效均 fail closed。

当前 Trust Policy 仍由进程外配置提供；独立标准分发、key rotation channel 和离线企业 trust bundle 属于 ARC-07 后续工作。

## Privacy-bounded Installation Credential

Registry 为每台已注册安装签发 `ReleaseManagedInstallationCredential`：

- installation Ed25519 public key 与 SHA-256，用于后续 proof-of-possession，不包含 private key；
- `member_id` 由 Registry-only pseudonym key 对 channel + installation public-key digest 做 domain-separated HMAC；
- channel、注册时间、过期时间与 exact registry signer identity；
- credential payload 与 detached Ed25519 signature；
- 明确 `raw_machine_identifier_collected=false`、`raw_user_identifier_collected=false`、`raw_path_collected=false`。

Registry signing key 与 pseudonym key 仅存在于内存 signer，不进入 artifact、SQLite 或日志。key rotation 会生成新的 credential
namespace；跨 key-generation 的稳定映射必须由 Registry 服务端迁移完成，客户端不得自行关联。

## Complete Population Snapshot

Snapshot 冻结：

- exact channel、sequence 与 previous snapshot ID/digest；
- 按 pseudonymous member ID 排序的完整 signed credentials；
- 去重后的 member/public-key/credential identity；
- `population_denominator == credentials length`；
- generated/valid/expires window；
- Registry 对完整 canonical payload 的 detached Ed25519 signature；
- `complete_registry_export=true`、`deleted_installations_excluded=true`、`raw_identifiers_excluded=true`。

每个成员 credential 必须与 Snapshot 使用相同 Registry/channel，在 valid-from 前已注册，并在整个 snapshot validity window 后仍有效。
成员上限为 10,000，artifact 上限为 16 MiB；超限必须由后续分页/merkle snapshot 协议解决，不允许静默截断 denominator。

## Durable Store 与动态撤权

`ReleasePopulationSnapshotStore`：

- 写入前验证 Snapshot 与每个 Credential signature、Trust Policy 和完整成员约束；
- SQLite `BEGIN IMMEDIATE` 内核对 channel sequence/previous link，拒绝缺口、分叉与回退；
- 相同 content-addressed Snapshot 并发写入幂等收敛；
- View 每次重读 durable artifact、latest channel head、current Trust Policy 和 expiry；
- 新 Snapshot、Registry key revocation、artifact 篡改或到期会动态撤销 `population_snapshot_authority`。

Snapshot 始终固定 `cohort_assignment_authority=false` 与 `percentage_rollout_authority=false`。它只是可信 population 输入，不能被 UI
展示成“已分桶”或“已扩大流量”。

## 验收结果

- 128 个真实 Ed25519 installation public keys 生成唯一 pseudonymous credentials；
- 六个独立 Store 并发写入同一签名 Snapshot，收敛到同一 authority；
- 第二个 Snapshot 正确链接 previous，并让旧 Snapshot 失去 latest authority；
- 在空 Store 直接写 sequence 2 被 chain gate 拒绝；
- 重复 member/public key、无效 detached signature、SQLite artifact 篡改均失败关闭；
- Snapshot 到期或 Registry key 撤销后动态撤权；
- JSON 不包含本机路径或用户标识，private/pseudonym key 从不持久化；
- ruff、compile、公共 release import 与本模块测试通过；未运行全量测试。

## 当前边界与下一步

本模块实现客户端侧 exact ingest/store/view 与测试用内存 signer，不实现远端 Registry HTTP 服务、安装 key provisioning、credential
renewal/revocation API、10,000 以上的 Merkle 分页或 telemetry accounting。生产 Registry 必须在独立服务端部署 signer 和 pseudonym
key，客户端只分发 trust policy/public artifacts。

下一切片 EVO-05.5f5a 必须同时消费 current ARC-07.5c Snapshot 与 current EVO-05.5f4e Stage Entry View，并要求安装私钥对
exact assignment challenge 做 proof-of-possession；随后才能通过稳定 hash 决定 member 是否进入 limited percentage cohort。
