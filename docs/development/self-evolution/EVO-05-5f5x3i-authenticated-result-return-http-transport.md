# EVO-05.5f5x3i Authenticated Result Return HTTP Transport

## 目标与边界

[EVO-05.5f5x3h](EVO-05-5f5x3h-remote-finalization-result-return-worker.md) 已用 durable 双阶段
outbox 管理安装端 writer、Result 签名和 Receipt 收口，但生产 Runtime 仍只能注入同进程 Control Plane adapter。本切片为
`EvolutionStableRemoteFinalizationResultTransport` 提供真实跨机器实现：安装端通过独立 mTLS HTTP/1.1 客户端提交 exact signed
Submission，Control Plane 从自己的 durable Delivery 反查成员授权，重验并返回 canonical Finalization Receipt。

本切片不启动常驻安装 daemon，不提供 DNS/service discovery、证书热重载或 Population Receipt aggregation。服务端是可由后续
supervisor 托管的真实 component；当前只把 Result 网络边界及 Runtime 出站组合完整交付。

## 身份与权限边界

Result endpoint 不复用普通 API key 或浏览器 API surface。连接必须同时满足：

1. 安装端验证 Control Plane CA 与 hostname/SAN，并在同一 TLS 连接发送任何 Submission 前核对服务端叶证书 SHA-256 pin；
2. Control Plane 以 `CERT_REQUIRED` 验证安装端 CA，再检查叶证书是否已登记；
3. 仅“CA valid + 全局 allowlist”仍不够。服务端从 header Delivery ID 读取自己的 durable Delivery，取得
   `installation_member_id`，然后要求当前叶证书出现在该成员专属 current/next pin 集合；
4. 一个已授权安装端证书不能提交另一成员的 Result；未知证书在 endpoint 层 403，缺失证书在 TLS 层失败；
5. TLS 最低版本 1.2，禁止明文、跳过 hostname、CA-only fallback 或 pin mismatch 后降级；
6. 私钥只接受普通文件；POSIX 拒绝 group/world 可读私钥，Windows 由部署 ACL 负责；日志和错误不输出路径、证书身份、body 或 traceback。

成员授权表由 `StableRemoteFinalizationResultHTTPServerPolicy.installation_certificate_sha256_by_member` 显式提供，每个成员最多两个
pin，用于 current/next 有界轮换。Control Plane server policy 不从安装端请求体接受成员身份，因此客户端不能自报或改写权限域。

## 线协议

固定 endpoint：

```text
POST /v1/stable-finalization/results:ingest HTTP/1.1
Content-Type: application/vnd.naumi.stable-finalization-submission.v1+base64
Accept: application/vnd.naumi.stable-finalization-receipt.v1+base64
X-Naumi-Delivery-ID: evstableremotedelivery_...
X-Naumi-Late-Recovery: true|false
Content-Length: ...
Connection: close
```

Body 是 x3d/x3h canonical Base64 `EvolutionStableRemoteFinalizationSubmission`；成功响应是 canonical Base64
`EvolutionStableRemoteFinalizationReceipt`。新增 Receipt codec 复用原有 512 KiB canonical artifact 边界。

`X-Naumi-Late-Recovery` 必须为严格小写布尔值，但它不授予 recovery authority。Control Plane 使用自己的 clock 和 durable Grant
expiry 重新计算是否走 late ingest，避免安装端选择更宽松的校验路径，也避免发送前后跨 expiry 的竞态。

服务端继续机械验证：Delivery 存在、成员证书绑定、Submission 与 execution package/grant/authorization/member 完全一致、原
Authorization/Probe/Trust Policy/Credential 仍有效、installation signature 正确、Delivery 已 ACK、single-use facts 未冲突。客户端只在
Receipt 的 execution package 和 exact Submission 与本次请求完全相同后完成 outbox。

同一 Submission 重放返回 exact same Receipt；同一 Grant 已绑定不同 Submission 仍冲突。稳定 JSON 错误码可被客户端有界解析并纳入
transport failure code，响应不包含内部异常文本。

## 晚到 Result 的有界收口

x3i 的真实网络测试发现：Result 若在 Grant 窗口内已完成并签名，但第一次网络回传失败，重试跨过 expiry 后，旧规则只接受“expiry
后补签”的 recovery Submission，会错误拒绝这个按时签名但晚到的事实。

本切片把 late ingest 窗口明确分成两类，二者都必须在 `_MAX_LATE_RECOVERY_SECONDS` 内且 writer completed time 位于原 Grant：

- delayed timely Submission：`issued <= completed <= signed < expiry <= control-plane now`；
- recovered durable writer：`issued <= completed < expiry <= signed <= control-plane now`。

前者没有扩大 writer 或签名权限，只允许已在原窗口内完成的不可变 Submission 完成网络传输；后者仍要求 x3e 的 existing-fact-only
补签。超过 recovery window、缺少 durable writer 或来源不再 current 均失败关闭。

## 有界 HTTP 与并发

- endpoint URL 必须是固定路径 https URL，拒绝 userinfo、query、fragment 和非 canonical path；
- 请求拒绝非 HTTP/1.1、非 POST、`Transfer-Encoding`、`Expect`、缺失/重复/非十进制/超限 `Content-Length`；
- request header 数量、单行与总字节均有上限；Submission/Receipt 分别不超过 512 KiB；
- 客户端严格解析单值响应 header，拒绝 Transfer-Encoding、重复 header、truncated body 和超限响应；
- connect、TLS handshake、whole request、Control Plane lookup/ingest 均有 deadline；
- 复用 x3g 已验证的 bounded TLS handler：握手也占并发槽并有 timeout，慢 ClientHello 不阻塞 accept loop；
- endpoint 以 semaphore 限制并发，以叶证书指纹执行每分钟滑动窗口限流；容量耗尽时不进入 Delivery Service；
- `Connection: close`、HSTS、CSP、no-store、nosniff 与 DENY 保持启用；
- `408/425/429/5xx` 和连接中断可重试；身份、pin、协议、artifact、member/Receipt mismatch 为 permanent。

## Runtime 配置

安装端出站配置位于 `harness.stable_remote_finalization_result_http_transport`：

```yaml
enabled: true
endpoint_url: https://control.example/v1/stable-finalization/results:ingest
server_ca_path: /secure/path/control-plane-ca.pem
client_certificate_path: /secure/path/installation.pem
client_private_key_path: /secure/path/installation.key
server_certificate_sha256_pins:
  - <current control-plane leaf sha256>
  - <next control-plane leaf sha256>
connect_timeout_seconds: 5
request_timeout_seconds: 15
max_response_bytes: 524288
```

配置了任何端点/证书却未显式启用、字段不完整、pin 不是 1–2 个小写 SHA-256、request timeout 小于 connect timeout，或 request
timeout 不小于 x3h Result Worker timeout，均在 Pydantic/Runtime composition 阶段失败关闭。`RuntimeServiceOverrides` 仍优先于默认
组合，便于测试和嵌入场景显式注入；未启用时不创建伪网络 transport。

Control Plane 入站 server policy 由部署层显式提供 server cert/key、installation CA、member→current/next leaf pin 映射、请求大小、
TLS/request timeout、速率和并发上限；本切片不把该敏感成员映射写入普通用户配置或 SQLite。

## 跨平台范围

实现使用 Python 标准库 `asyncio`、`ssl`、`http.server`、`socket`、`threading` 与 `pathlib`，不依赖 Unix socket、fork、signal 或
shell。真实 loopback 在当前 macOS/Python 环境动态生成 CA、Control Plane 与两个 installation client 证书。Linux/Windows 发布矩阵、
OS service manager 和证书 ACL/热重载证据属于后续 daemon/发布切片，当前不能以“代码可移植”冒充三平台生产认证。

## 验收证据

- [x] 真实 mTLS loopback 完成 Result Worker→Control Plane→exact Receipt 全链路；
- [x] 重复 exact Submission 返回同一 Receipt，安装 outbox 与 Control Plane Delivery 一致；
- [x] 服务端叶证书在 body 发送前同连接 pin，错误 pin 为 permanent；
- [x] CA-valid 安装证书仍必须绑定 exact Delivery member，跨成员证书被拒绝；
- [x] current/next 安装端证书轮换可用，未登记或缺失客户端证书失败关闭；
- [x] Control Plane clock 权威选择 normal/late ingest，按时签名但晚到的 Result 可在有界窗口收口；
- [x] 重复 Content-Length、响应大小上限和安全错误码均有真实网络证据；
- [x] 并发上限为 1 时最多一个请求进入 Delivery lookup，额外连接形成 retryable transport failure；
- [x] partial config、unsafe timeout、固定 endpoint 和证书文件策略 fail closed；
- [x] Runtime composition 从配置自动构建 x3h Result Worker 所需 transport；
- [x] 相邻 x3g/x3h/Delivery/Engine/Config 小模块回归、Ruff、compile、lazy exports 与文档治理通过；
- [x] 按用户要求未运行全量测试。

## 自我审视与下一步

x3i 已完成单安装端 Result 的跨机器认证传输，但仍不能宣称 fleet stable rollout 完成：

- 两个 endpoint 仍由调用方手动 start/stop，缺少安装端进程监督、健康/心跳、崩溃重启、service discovery 与优雅 drain 编排；
- member→certificate 映射是显式部署输入，尚无证书注册/撤销分发、热重载、OCSP/CRL 或 SPIFFE 控制面；
- rate limiter 为 endpoint-local；多副本部署需共享网关/限流 authority；
- dead-letter 的签名审查、requeue/abandon 和 retention 尚未接入 Result Worker；
- Linux/Windows 真实 service 与网络故障矩阵仍缺发布证据；
- 单 member Receipt 尚未聚合为 Population Stable Rollout Completion Authority。

[HAR-10.9a Remote Finalization Installation Daemon Supervision](../harness/HAR-10-9a-remote-finalization-installation-daemon-supervision.md)
已把 x3g server、x3h Worker、x3i transport、RunLease、heartbeat 与原子 discovery 组合成可恢复的安装端
daemon。下一切片回到 EVO-05.5f5x3j，交付逐 member Receipt aggregation 和 Population completion authority。
