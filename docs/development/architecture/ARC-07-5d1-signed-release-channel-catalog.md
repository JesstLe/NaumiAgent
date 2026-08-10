# ARC-07.5d1 Signed Release Channel Catalog

## 目标

为 closed-source updater 建立独立于 release archive 的可信更新发现权威。客户端不再接受任意 URL、版本字符串或本地
自报 candidate，而是从 Ed25519-signed、hash-chained Channel Catalog 中解析当前 channel 和 host target 对应的 exact
archive/build identity。

本切片是 EVO-05 percentage deployment 的最小 distribution 前置，只产生 download input；不执行网络请求、解压、安装、
active pointer 切换、进程启动或 rollout。

## 双信任根

Catalog 同时验证两条相互独立的信任链：

1. Channel Trust Policy：决定哪些 channel signer key 可发布 `stable` 等 channel，并冻结 key generation、签署窗口、撤销状态
   和允许的 channel；
2. Build Trust Policy：验证 Catalog 中每个 embedded ARC-07.4b Build Attestation 的 builder identity、有效窗口和 Ed25519
   signature。

Channel signer 不能替代 builder 证明伪造 archive/source provenance；builder signer 也不能绕过 channel policy 自行宣布某个
build 已进入 stable。两份公开 Trust Policy 都由 launcher/platform installer/管理员控制通道独立提供，私钥从不持久化。

## Catalog 契约

每个 Catalog 冻结：

- signer identity、channel、严格递增 sequence 与 previous catalog ID/digest；
- 每个 target 唯一的一条 current Entry；
- version、单调 `release_generation`、安全相对 archive path、archive name/size/SHA-256、manifest SHA-256；
- embedded exact Build Attestation；
- generated/valid/expires 时间窗口；
- Ed25519 signature 与 content-addressed Catalog identity。

Entry 必须与 Build Attestation 的 target/version/archive/manifest projection 完全一致。Archive origin 不由 Catalog 控制，而由
独立 Channel Trust Policy pin；Catalog 只提供不含 absolute、`..`、反斜杠、query、fragment 或控制字符的安全相对路径。

## Durable Store 与 rollback resistance

- SQLite Store 对每个 channel 保存 append-only sequence chain，`BEGIN IMMEDIATE` 保证多进程并发幂等；
- Catalog 写入前后均重新验证 channel/build trust 和密码学 signature；
- 新 Catalog 必须精确链接 current tail；chain gap、fork 和 identity conflict 全部 fail closed；
- 对连续 Catalog 中仍存在的 target，`release_generation` 必须严格增加，普通 updater discovery 不能伪装成版本回退；
- 历史 Catalog 可审计，但一旦不是 latest 就失去 resolution/download-input authority；真正 rollback 继续走 ARC-07.5a 和
  EVO-05.6 的独立受控回滚链。

动态 View 还会重验 Catalog validity、channel key 撤销、Build Trust Policy 轮换/撤销以及 durable JSON identity。任一变化都即时
撤销历史 authority。

## Target Resolution

`resolve(channel, target)` 只在 latest Catalog 全部 current checks 成立时生成 content-addressed Resolution，冻结：

- exact Catalog、Channel Trust Policy 与 Build Trust Policy ID/digest；
- exact Entry、target 与由 pinned origins 组合出的 HTTPS archive URLs；
- `catalog_resolution_authority=true` 与 `download_input_authority=true`。

以下能力始终为 false：

- `download_executed`；
- `installation_authority`；
- `deployment_authority`。

解析结束前再次检查 latest Catalog，避免解析过程中 channel tail 更新后返回 stale Resolution。

## 验收结果

- 使用真实 Ed25519 channel signer 与 builder signer 生成 Build Attestation 和 Catalog；
- 四个独立 Store 并发写入同一 Catalog 收敛到同一 durable View；
- pinned 双 HTTPS origin 产生 deterministic target Resolution；
- next Catalog 使历史 Catalog 动态撤权；
- channel key 撤销和 builder key 撤销分别撤销 resolution authority；
- 错误 Catalog signature、未知 target、path traversal、chain gap、release generation 回退与 SQLite JSON 篡改全部 fail closed；
- Channel Catalog 与 Build Attestation 两个小模块共 7 项测试通过；未运行全量测试。

## 后续切片

[ARC-07.5d1a Channel Trust Policy Loader](ARC-07-5d1a-channel-trust-policy-loader.md) 已补齐安装级
`trusted-channels.json` 的 bounded、no-symlink、identity-stable 加载入口，为 Production Engine 组合真实 Catalog
Store 提供公开信任根。它不改变 Catalog 内容、下载或部署权限。

[ARC-07.5d2 Verified Artifact Fetch](ARC-07-5d2-verified-artifact-fetch.md) 已消费 current Resolution，以受限 HTTPS
transport、有界 raw stream、claim fencing、同文件系统 staging、fsync 和 atomic rename 形成动态撤权 Download Receipt。

[ARC-07.5d3 Verified Archive Admission](ARC-07-5d3-verified-archive-admission.md) 已进一步完成安全解包、重验
manifest/Build Attestation，并通过 ARC-07.5a 安装 immutable inactive slot。下一最小切片是
`EVO-05.5f5b Percentage Deployment Intent`，它仍不能把 intent 声称为 rollout 已发生。
