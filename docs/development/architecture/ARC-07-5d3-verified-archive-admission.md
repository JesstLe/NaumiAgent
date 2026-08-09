# ARC-07.5d3 Verified Archive Admission

## 目标

消费 current ARC-07.5d2 `ReleaseArtifactDownloadReceipt`，把 signed archive 安全解包、重验 ARC-07.4b Build
Attestation/manifest，并真实安装为 ARC-07.5a immutable inactive slot。最终形成可动态撤权的
`ReleaseArchiveAdmissionReceipt`，供 percentage deployment intent 使用。

本切片不执行 boot probe、不切换 active pointer、不启动进程，也不声称 deployment 或 rollout 已发生。

## 安全解包边界

Admission 只接受 signed Entry 声明的 exact `tar.gz` 或 `zip`，并要求 archive 中只有与 archive name 对应的单一顶层
bundle。实现不调用 `extractall()`，而是先收集和验证 member，再逐文件独占写入隔离 staging：

- 禁止 absolute、`.`、`..`、反斜杠、控制字符、冒号、超长 component/path；
- 拒绝 Windows reserved device name、尾随空格/点，以及 Unicode NFC + casefold 后的跨平台路径碰撞；
- 只允许 directory、regular file 和受限 symlink；拒绝 hardlink、device、FIFO、sparse member 与 encrypted ZIP；
- symlink 必须使用相对 target，词法上留在 exact bundle root，创建后再次 strict resolve，拒绝越界、循环和失效链接；
- 任一 file/symlink 不能成为其他 member 的父节点；所有 file 使用 `xb`，不能覆盖先前路径；
- member 数量最多 100000，路径最多 4096 bytes；声明/实际 member size 必须一致；
- 总解压字节受 8 GiB hard ceiling、200 倍 expansion ratio 与 64 MiB slack 的共同上限约束；
- ZIP CRC、tar/zip 完整性、member 实际字节数和最终单一 bundle directory 全部验证。

这些规则按跨平台最严格交集执行，避免同一 archive 在 macOS、Linux 和 Windows 上解析为不同文件集合。

## 构建证明与 immutable slot

安全解包后，Service 使用 current Build Trust Policy 对 Catalog 内嵌的 exact Ed25519 Build Attestation 再验签，并同时
绑定：

- 原始已验证 archive SHA-256；
- 解包后的 exact `manifest.json` SHA-256；
- product/version/target/source commit/source tree projection；
- active builder key、key generation、有效窗口和撤销状态。

通过后才调用 `ReleaseSlotStore.install()`，以 signed manifest digest 作为安装前置条件。Slot 安装现在在 copy 和 immutable
chmod 后分别 fsync 完整文件树，再执行 atomic rename 和目录 fsync；安装完成后公开 `inspect_installed_slot()` 会重新验证
manifest、完整文件集合、每个摘要、source-free 约束和只读状态。

解包/安装结束后再次重验 Download Receipt、Build Trust Policy、Build Attestation 与 active pointer。若 candidate slot 已经是
active slot，则拒绝形成新的 inactive Admission。

## Durable Receipt 与并发恢复

Admission Store 与 Download/Catalog 共用 exact SQLite DB。写入事务会机械读取并比较 durable Download Receipt，随后按
`download_source_id` 唯一写入 content-addressed Receipt；多个 Service 并发解包和安装最终收敛到同一 immutable slot 与
Receipt。

Receipt 冻结 exact Download Receipt、Build Trust Policy、trusted builder key、archive format、member/解压字节计数和完整
installed slot。若进程在 slot install 后、Receipt 写入前崩溃，重试会重新验证 archive，并复用 already-installed exact slot，
不会切换 active pointer或重复生成版本槽。

动态 View 会重新验证：

- durable Admission 与 Download Receipt；
- current Catalog/Channel/Build Trust 和 archive bytes；
- installed slot JSON、manifest、文件集合、摘要和 immutability；
- candidate slot 是否仍为 inactive。

前四类权威成立时 `archive_admission_authority=true`；candidate 仍 inactive 时才额外产生
`percentage_deployment_intent_input_authority=true`。

## 验收结果

- 真实生成并签署的 `tar.gz` 与 Windows `zip` 均完成 fetch、隔离解包、manifest/Attestation 重验和 immutable-slot 安装；
- 三个并发 Service 收敛到同一 Receipt 和 slot，active pointer 保持为空；
- 删除 Admission DB row 后从 existing exact slot 完成崩溃恢复；
- path traversal、symlink escape、hardlink、大小写碰撞和错误 manifest 在安装前拒绝；
- expansion limit、Windows reserved name、尾随空格/点、反斜杠与 `..` 路径全部拒绝；
- Build key 撤销、download archive 篡改、installed runtime 篡改和 Receipt JSON 篡改均动态撤权或 fail closed；
- ruff、compile、公共 import、静态自审及相关 release 小模块 27 项测试通过；未运行全量测试。

## 当前边界与下一步

Admission 已产生 exact current inactive candidate slot，但没有 installation-specific population selection binding、durable user intent、
bootability、activation CAS 或 exposure evidence。下一最小切片回到 `EVO-05.5f5b Percentage Deployment Intent`：它必须组合
current percentage Assignment、current Archive Admission、managed installation credential/target 与当时的 previous pointer CAS，
只签发一次短期 intent。Intent 仍不能被解释为 activation 或 rollout；后续 executor 必须 boot candidate、原子切换并形成独立
Deployment Receipt。
