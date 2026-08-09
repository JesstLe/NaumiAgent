# ARC-07.5d2 Verified Artifact Fetch

## 目标

把 ARC-07.5d1 的 current `ReleaseChannelResolution` 转换为可供后续安全解包使用的、可动态撤权的
`ReleaseArtifactDownloadReceipt`。本切片只负责可信下载和 content-addressed 原子落盘；不解包、不安装、不切换 active
slot，也不产生 rollout/deployment authority。

## 输入权威

Fetch 只接受已经由 `ReleaseChannelCatalogStore.resolve()` 产生的 Resolution，并在下载前后重新验证：

- exact latest Catalog ID/SHA-256 与 target Entry；
- Channel/Build Trust Policy ID/SHA-256 和当前有效性；
- 由 Channel Trust Policy pin 的完整 HTTPS origin 集合；
- archive name、size、SHA-256 和 embedded Build Attestation projection。

Catalog 更新、签名 key 撤销、Trust Policy 轮换或 origin 改变都会令旧 Resolution 失效。URL 不能由调用方覆盖，transport
也不会跟随 redirect。

## 有界 transport

`HttpxReleaseArtifactTransport` 使用独立异步客户端，固定 `Accept-Encoding: identity`、禁止 redirect，并配置 connect/read/write/
pool timeout。每个 origin 必须同时满足：

- HTTP 200；
- 无压缩或 identity encoding；
- 合法且与 Catalog exact size 相等的 `Content-Length`；
- raw stream 实际字节数不超过且最终等于 exact size；
- streaming SHA-256 与 Catalog archive digest 恒等。

下载以 256 KiB 上限的 raw chunk 消费；缺失/错误长度、截断、超长、摘要不符、transport error 和非 200 响应均 fail
closed，并按 Resolution 中的 deterministic origin 顺序尝试 fallback。

## 并发、fencing 与崩溃恢复

Download Store 与 Channel Catalog 共用 exact SQLite DB，并通过 `BEGIN IMMEDIATE`、owner、单调 claim epoch 和可续租 lease
保证多进程只有一个执行者拥有写入权：

- 同一进程再用 per-source async lock 合并并发请求；
- lease 到期后新执行者提升 epoch，旧执行者不能续期或提交 Receipt；
- 等待者有明确上限，不无限阻塞；
- 异常、取消和失败 origin 均删除 `.part` staging；
- 若进程在 atomic rename 后、Receipt commit 前崩溃，下一执行者会重新验证 exact content-addressed 文件并补写 Receipt，
  不重复下载。

## 原子落盘与 Receipt

Artifact 写入目标目录内的独占 staging 文件，完成后依次执行 file flush/fsync、只读权限、再次 file fsync、`os.replace` 和
directory fsync。最终路径由 archive SHA-256 与安全 archive name 派生，不能由网络响应控制。

Receipt 冻结：

- exact Resolution、source identity、claim owner hash/epoch；
- 每个 origin 的隐私化 URL SHA-256、HTTP/长度/摘要结果、实际字节数、chunk 数和耗时；
- 最终 archive canonical path、size、SHA-256；
- file/directory fsync、atomic commit、只读和验证事实；
- `extraction_executed=false`、`installation_executed=false`、`deployment_authority=false`。

Receipt 使用 content identity 并持久化到 SQLite。动态 View 会重新验证 durable Receipt、current Resolution/Catalog/Trust
Policy 以及磁盘文件；任一漂移都会撤销 `verified_archive_authority` 和 `installation_input_authority`。

## 验收结果

- 四个独立 Service/Store 并发请求同一 Resolution，只产生一次真实 HTTP stream 并收敛到同一 Receipt；
- 首 origin 失败后 deterministic fallback 到第二 origin；
- redirect、缺失 Content-Length、压缩编码、截断、超长和错误 SHA-256 全部拒绝，且无残留文件；
- claim 到期后旧 owner 被 fencing，不能续租或提交；
- atomic file 已存在但 Receipt 未写入时完成无网络崩溃恢复；
- archive 文件篡改、next Catalog 和 SQLite Receipt 篡改都会即时撤权或 fail closed；
- ruff、compile、公共 import、静态自审与相关 release 小模块 11 项测试通过；未运行全量测试。

## 当前边界与下一步

Download Receipt 证明的是 archive bytes 已可信落盘，不证明 archive 内路径安全、manifest 完整、平台签名有效或 bundle 已安装。
下一最小切片 `ARC-07.5d3 Verified Archive Admission` 应消费 current Download Receipt，在隔离 staging 中防止 absolute path、
`..`、symlink/hardlink、设备文件、大小/文件数炸弹和覆盖攻击，重验 ARC-07.4b manifest/Build Attestation 后，才允许把
validated bundle 交给 ARC-07.5a immutable inactive-slot installation。该 Receipt 完成前不能实现
`EVO-05.5f5b Percentage Deployment Intent`。
