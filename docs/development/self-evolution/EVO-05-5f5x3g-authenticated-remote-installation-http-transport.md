# EVO-05.5f5x3g Authenticated Remote Installation HTTP Transport

## 目标与边界

[EVO-05.5f5x3f](EVO-05-5f5x3f-remote-finalization-delivery-worker.md) 已提供 durable outbox、周期 Worker 与认证安装传输
Protocol，但生产 composition 仍只能注入本机 adapter。本切片交付跨主机可用的 HTTPS/mTLS 客户端、独立安装端 endpoint、
current/next 证书 pin 轮换、严格 HTTP 边界，以及配置驱动的 Runtime 自动装配。

本切片只传输 x3e 的 signed Delivery Package 并返回 installation-signed ACK。ACK 仍固定为
`writer_executed=false`；HTTP endpoint 不执行 Release Store writer，不生成 Finalization Receipt，也不扩大现有 Execution Grant。
Result 主动回传、安装端 daemon 的进程监督/service discovery 与 Population Receipt aggregation 仍是后续独立切片。

## 威胁模型与身份边界

该端点不复用普通 FastAPI/CORS/API-key surface。原因是安装 finalization 属于控制面能力，必须同时证明：

1. 客户端信任服务端 CA、hostname/SAN，并在**同一 TLS 连接发送 package 前**核对服务端叶子证书 SHA-256 pin；
2. 服务端以 `CERT_REQUIRED` 验证客户端 CA，再以客户端叶子证书 SHA-256 allowlist 绑定 Control Plane identity；
3. TLS 最低版本为 1.2，系统默认 cipher policy 保持启用，不提供明文或跳过证书验证的 fallback；
4. 证书私钥只能来自普通文件；POSIX 上拒绝 group/world 可读私钥，Windows 由 ACL/证书部署负责；
5. 文件路径、证书与 pin 只作为配置引用，源码、日志、HTTP 错误和 durable journal 均不保存私钥。

服务端和客户端 pin 均支持最多两个值，用于 current/next 有界轮换；未知 pin 是 permanent failure，不能通过重试降级。

## 线协议

固定 endpoint：

```text
POST /v1/stable-finalization/deliveries:receive HTTP/1.1
Content-Type: application/vnd.naumi.stable-finalization-delivery.v1+base64
Accept: application/vnd.naumi.stable-finalization-delivery-ack.v1+base64
X-Naumi-Delivery-ID: evstableremotedelivery_...
Content-Length: ...
Connection: close
```

Body 直接使用 x3e canonical Base64 package；成功响应直接返回 canonical Base64 ACK。endpoint 要求 header delivery ID 与 package
完全一致，并继续由 `EvolutionStableRemoteFinalizationTargetJournal` 重验 Rollout Control Trust Policy、member binding 和 exact package。
同一 package 重放返回 journal 中完全相同的 ACK；同一 delivery ID 携带不同 package 触发 conflict。

协议拒绝：

- 非固定 https URL、userinfo、query、fragment 或不同路径；
- 非 HTTP/1.1、非 POST、`Transfer-Encoding`、`Expect: 100-continue`；
- 缺失、重复、非十进制或超限 `Content-Length`；
- 错误 media type、非法 ASCII/Base64/Pydantic artifact、header/body identity mismatch；
- 超过 2 MiB 的 package 或超过配置上限的 ACK；
- 响应中的重复 header、Transfer-Encoding、超长 header/line、缺失 Content-Length 或 truncated body。

客户端把 `408/425/429/5xx` 归为 retryable，把认证、协议、identity 和 artifact 错误归为 permanent。服务端错误只返回稳定 code，
不包含异常文本、路径、证书身份或 traceback。

## 有界执行与抗滥用

- connect timeout 和 whole-request timeout 分开约束；request timeout 必须小于 x3f Worker ACK timeout；
- TLS handshake 在受并发 semaphore 约束的 worker 线程中执行，并有独立 timeout；慢握手不会阻塞 accept loop；
- 服务端 socket 读取和目标 `receive()` 处理都受 request timeout 约束；
- 每个已授权客户端证书指纹有独立的 60 秒滑动窗口速率限制；
- endpoint 以有界 semaphore 限制并发 handler；容量耗尽时在进入 target journal 前关闭额外连接，客户端按 retryable transport failure 处理；
- `Connection: close` 避免请求走私跨请求复用状态；
- 响应包含 `HSTS`、`CSP default-src 'none'`、`nosniff`、`DENY` 和 `no-store`；
- handler 异常不打印 peer identity、请求内容或 stack 到 stderr；
- start/stop 有 lifecycle lock，TLS 初始化失败会关闭已绑定 socket，stop 有界 join 且可判定幂等。

## Runtime 配置

Control Plane 出站配置位于 `harness.stable_remote_finalization_http_transport`：

```yaml
enabled: true
endpoint_url: https://installation.example/v1/stable-finalization/deliveries:receive
server_ca_path: /secure/path/installation-ca.pem
client_certificate_path: /secure/path/control-plane.pem
client_private_key_path: /secure/path/control-plane.key
server_certificate_sha256_pins:
  - <current leaf certificate sha256>
  - <next leaf certificate sha256>
connect_timeout_seconds: 5
request_timeout_seconds: 15
max_response_bytes: 524288
```

配置了端点或证书却未 `enabled`、启用后字段不完整、pin 不是 1–2 个小写 SHA-256、request timeout 不小于 Worker ACK
timeout，都会在配置/Runtime composition 阶段失败关闭。未启用时不创建网络 transport；测试和嵌入场景仍可通过
`RuntimeServiceOverrides.stable_remote_finalization_transport` 注入显式实现。

安装端使用 `StableRemoteFinalizationHTTPServerPolicy` 显式提供服务端 cert/key、客户端 CA、current/next 客户端证书指纹、
请求大小、TLS handshake/处理 timeout 和每分钟限额；`StableRemoteFinalizationHTTPServer` 复用 x3f 的本机安装 adapter，不能绕过 target journal。
服务端 policy 另含 `max_concurrent_requests`，默认 32，范围 1–1024。

## 跨平台实现

网络和 TLS runtime 只使用 Python 标准库 `asyncio`、`ssl`、`http.server`、`socket` 和 `threading`，没有依赖 Unix socket、fork、
signals 或 shell 命令；路径使用 `pathlib.Path`，Windows 不把 POSIX mode bit 当作 ACL 证据。真实 loopback 测试会临时生成 CA、
服务端与客户端 ECDSA 证书，覆盖当前执行平台。macOS/Linux/Windows CI matrix 仍需在发布流水线分别运行，当前 macOS 本机通过不等于
三端发布认证已经完成。

## 验收证据

- [x] 真实 TLS loopback 完成 CA/hostname、客户端证书与双向 leaf pin 验证；
- [x] 正确 package 经目标 Trust Policy 和 durable journal 返回 installation-signed ACK；
- [x] 同一 package 重放返回 exact same ACK，且 `writer_executed=false`；
- [x] 错误服务端 pin 在 package 发送前失败，目标 journal 没有写入；
- [x] current/next 双 pin 接受 next 服务端证书，未知 pin 被拒绝；
- [x] 缺少客户端证书在 TLS 层失败，CA-valid 但未授权客户端在 endpoint 层 403；
- [x] 超限 body、重复 Content-Length、Expect 和目标处理 timeout 都有稳定、非泄漏结果；
- [x] 并发上限为 1 的真实 TLS 场景最多只进入一个 target handler，额外连接形成 retryable failure；
- [x] 不发送 TLS ClientHello 的慢连接在 handshake timeout 后释放槽位，listener 随后可正常处理认证请求；
- [x] 私钥权限、非 canonical endpoint、partial config 和 unsafe timeout fail closed；
- [x] Runtime composition 能从配置自动构建 x3f Worker 所需 transport；
- [x] 相关 x3f Worker/Engine 回归、Ruff、compile 与 public lazy API smoke 通过；
- [x] 按用户要求未运行全量测试。

## 自我审视与下一步

本切片使 package/ACK 真正跨越认证网络边界，但还没有把整条 fleet finalization 宣称为完成：

- 安装端 server 已是可启动的真实 endpoint component，但尚未加入独立 daemon/service manager、证书热重载和 service discovery；
- 证书 pin 轮换是 current/next 配置切换，不包含在线 OCSP/CRL 或 ACME/SPIFFE 控制面；
- 当前 loopback 在 macOS/Python 3.14 执行，Linux/Windows 需要发布 CI 的真实平台证据；
- ACK 后 writer/result 仍不会被 endpoint 自动执行或回传，这是刻意保留的 authority separation；
- rate limiter 是单进程 endpoint-local，未来多副本部署必须绑定共享或网关层限流策略。

[EVO-05.5f5x3h Remote Finalization Result Return Worker](EVO-05-5f5x3h-remote-finalization-result-return-worker.md) 已消费 ACKed
target journal，以 durable 双阶段 outbox 监管 expected-pointer writer/补签、Result 回传和 Control Plane Receipt。
[EVO-05.5f5x3i](EVO-05-5f5x3i-authenticated-result-return-http-transport.md) 已进一步交付 installation→Control Plane 的 member-bound
mTLS Result transport；daemon packaging 和三平台 deployment matrix 仍作为后续独立 Harness 运维切片，不能把单 member 网络闭环声明成
Population 完成。
