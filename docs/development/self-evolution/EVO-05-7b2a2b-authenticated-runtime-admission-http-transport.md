# EVO-05.7b2a2b Authenticated Runtime Admission HTTP Transport

## 目标与边界

[EVO-05.7b2a2a](EVO-05-7b2a2a-fenced-runtime-admission-delivery-worker.md) 已完成 durable dispatch、owner/epoch/lease
fencing、Receipt ACK、retry/dead-letter 和 transport Protocol，但 production composition 只能显式注入本地 adapter。本切片交付
installation→Control Plane 的真实 HTTPS/mTLS client、独立 Control Plane endpoint、成员证书绑定、current/next pin 轮换、严格
HTTP 边界与配置自动装配。

本切片只把 7b2a1 signed Submission 传到 Control Plane，并返回 exact 7b2a1 Receipt。它不读取长期 heartbeat window、不计算 fleet
metrics、不签发 promoted/superseded Outcome，也不授予 learning、promotion 或 execution authority。

## 威胁模型与身份绑定

协议必须同时证明三层身份：

1. Submission 已由 current Population Credential 对独立 domain
   `naumi.release.stable-promotion-runtime-admission.v1` 签名；
2. installation client 在 TLS 握手中提供 CA-valid leaf certificate，Control Plane 把 leaf SHA-256 pin 映射到 Submission 的 exact
   `installation_member_id`；
3. installation client 在发送 body **之前**，对同一 TLS connection 的 Control Plane leaf certificate 执行 current/next pin 校验，
   同时保留 CA 与 hostname/SAN 验证。

Control Plane 证书 CA-valid 但 pin 未知时，client 不发送 Submission。installation 证书 CA-valid 但映射到其他 member 时，endpoint
返回 permanent 403。没有客户端证书时在 TLS 层失败。任何路径都不提供明文、跳过验证或未知 pin 降级。

证书私钥只能通过配置文件路径引用。POSIX 上复用 shared TLS validator 拒绝 group/world-readable 私钥；Windows 不把 POSIX mode
误当作 ACL 证据。源码、日志、HTTP error 和 durable SQLite 都不保存证书私钥。

## 固定线协议

```text
POST /v1/stable-promotion/runtime-admissions:receive HTTP/1.1
Content-Type: application/vnd.naumi.stable-promotion-runtime-admission.v1+base64
Accept: application/vnd.naumi.stable-promotion-runtime-admission-receipt.v1+base64
X-Naumi-Admission-ID: evstablepromadmit_...
X-Naumi-Submission-ID: evstablepromsubmit_...
Content-Length: ...
Connection: close

<canonical Submission Base64>
```

成功响应 body 是 canonical Receipt Base64。Receipt codec 与 Submission 一样执行严格 Base64、canonical round-trip、Pydantic
`extra=forbid` 和 64 KiB decoded artifact 上限；HTTP envelope 另限制 128 KiB request/response。

endpoint 拒绝：

- 非固定 https URL、userinfo、query、fragment、错误 path 或非法 port；
- 非 HTTP/1.1、非 POST、`Transfer-Encoding`、`Expect: 100-continue`；
- 缺失、重复、非十进制或越界 `Content-Length`；
- 重复/超长/过量 headers、非法 Host、错误 media type；
- Admission/Submission header 与 body artifact identity 不一致；
- 非 canonical Base64、未知字段、签名/lineage/current authority 失败；
- Receipt 与 Submission identity 不一致、响应缺少长度、重复 header、截断或超限。

服务端只返回稳定 error code，不回显异常文本、路径、证书 fingerprint、Submission 内容或 traceback。客户端将 `408/425/429/5xx`
归为 retryable，协议、identity、artifact 和 authority `4xx` 归为 permanent，由 7b2a2a Worker 按 durable budget settlement。

## 有界并发与抗滥用

- connect timeout 与 whole-request timeout 分离，request timeout 必须严格小于 Worker Receipt timeout；
- TLS handshake 在 shared `BoundedTLSHTTPServer` 的受限槽位中执行并有独立 timeout；
- 每个已授权 installation leaf fingerprint 使用独立 60 秒滑动窗口速率限制；
- endpoint semaphore 限制并发 handler，容量耗尽时不会进入 7b2a1 receive Service；
- `Connection: close` 禁止跨请求复用状态，降低 request smuggling 面；
- 响应包含 HSTS、CSP `default-src 'none'`、`nosniff`、`DENY`、`no-store`；
- start/stop 由 lifecycle lock 保护，TLS 初始化失败会关闭已绑定 socket，stop 有界 join 且幂等。

## Runtime 配置与装配

installation 出站配置位于 `harness.stable_promotion_runtime_admission_http_transport`：

```yaml
harness:
  stable_promotion_runtime_admission_delivery:
    enabled: true
    receipt_timeout_seconds: 20
  stable_promotion_runtime_admission_http_transport:
    enabled: true
    endpoint_url: https://control.example/v1/stable-promotion/runtime-admissions:receive
    server_ca_path: /secure/control-plane-ca.pem
    client_certificate_path: /secure/installation.pem
    client_private_key_path: /secure/installation.key
    server_certificate_sha256_pins:
      - <current-control-plane-leaf-sha256>
      - <next-control-plane-leaf-sha256>
    connect_timeout_seconds: 5
    request_timeout_seconds: 15
    max_response_bytes: 131072
```

配置了任意 endpoint/cert/pin 却未显式 `enabled`、启用但字段不完整、pin 不是 1–2 个小写 SHA-256、request timeout 不小于
Worker Receipt timeout，都会在 Pydantic/Runtime composition 阶段失败关闭。显式 `RuntimeServiceOverrides` 仍优先于配置，便于嵌入式
部署和测试。

Control Plane server 使用 `StablePromotionRuntimeAdmissionHTTPServerPolicy` 显式注入 server cert/key、installation CA、
`member_id -> current/next client leaf pins`、request/handshake timeout、请求大小、并发与速率限制。当前 server 是可启动组件，但不在本
切片虚构 daemon/service discovery 已完成。

## 验收标准

- [x] 真实 TLS loopback 完成 CA、hostname/SAN、双向 client/server leaf pin 验证；
- [x] signed Admission 经 7b2a2a Worker 跨 TLS 到达 7b2a1 Control Plane Service并形成 Receipt ACK；
- [x] 同一 Submission 重放返回 exact durable Receipt；
- [x] 错误 Control Plane pin 在 body 发送前失败；
- [x] current/next Control Plane pin 接受 next server certificate；
- [x] current/next installation pin 接受 next member certificate；
- [x] CA-valid 但绑定其他 member 的 installation certificate 被 permanent 拒绝；
- [x] 无客户端证书在 TLS 层失败；
- [x] 重复 Content-Length、响应大小上限和 canonical artifact 边界失败关闭；
- [x] 并发槽位饱和不会进入 receive Service，客户端得到 retryable failure；
- [x] 每 leaf 每分钟速率限制返回 retryable 429；
- [x] Runtime config 自动装配 7b2a2a Worker transport，partial/unsafe timeout 配置失败关闭；
- [x] public lazy API、Ruff、compile、YAML、diff check 与定向 pytest 通过；
- [x] 按用户要求未运行全量测试。

## 自我审视与下一步

本切片使 Runtime Admission 真实跨越认证网络边界，但没有扩大观测结论：

- Control Plane endpoint 尚未进入独立 daemon/service manager、证书热重载和 endpoint discovery；
- current/next pin 是有界配置轮换，不包含 OCSP/CRL、ACME 或 SPIFFE；
- rate limiter 是单进程本地状态，多副本部署需要共享网关/限流 authority；
- dead-letter 的人工签名 requeue/abandon 和 retention 尚未实现；
- 当前真实 loopback 只证明本机平台，Linux/Windows 仍需要发布 CI matrix 证据。

[EVO-05.7b3a](EVO-05-7b3a-stable-promotion-observation-chain-cursor.md) 已为每个 acknowledged Admission 从 installation
本地 HAR ledger 建立逐 sample content-addressed、可恢复的 durable cursor。下一步 7b3b 必须把 revision 签名传到 Control Plane；
只有 7b3c 完整 Population aggregation 达标，才能进入 7b4 promoted/superseded ledger。
