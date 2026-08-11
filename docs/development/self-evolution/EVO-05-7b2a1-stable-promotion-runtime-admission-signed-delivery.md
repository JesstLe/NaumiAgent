# EVO-05.7b2a1 Stable Promotion Runtime Admission Signed Delivery

## 目标

把 [EVO-05.7b2](EVO-05-7b2-stable-promotion-runtime-observation-admission.md) 在 installation 本地形成的
Runtime Admission，转换为可验证、可持久化、可幂等接收的签名 Submission。Control Plane 只在 current Population
Credential、exact Observation Contract、Population Finalization 与 member Receipt 全部有效时记录 Delivery Receipt。

本切片是 `EVO-05.7b2a` 的第一阶段。它建立跨 trust boundary 的 artifact 和接收 authority，但不声称已经存在生产级
HTTP 传输、后台重试 Worker 或 fleet 长期窗口。

## 为什么不能由 Control Plane 直接读取远端 Harness SQLite

7b2 的 startup origin 与 Runtime Identity 位于 installation 本地 Harness ledger。Control Plane 无权把远端路径当成本地路径
重新打开，也不能把“无法直接读取远端数据库”解释为“没有验证”。本切片采用如下证据边界：

1. installation 本地 7b2 Service 已对 Harness Binding、startup sequence-1、Stable Deployment 和 Finalization member 做动态
   复验，并持久化 content-addressed Admission；
2. installation 使用与 current Population Credential 对应的 Ed25519 私钥，对完整 Admission 与 `submitted_at` 的 canonical
   payload 签名；
3. Control Plane 从 Admission 指向的 exact current Population Snapshot 中解析 Credential，验证签名和 member lineage；
4. Control Plane 重读自身 durable Contract、Population Finalization 与 member Receipt 后才写入 Receipt；
5. Receipt 明确记录 `remote_harness_directly_revalidated=false`，不把签名 testimony 冒充远端数据库直读。

因此，签名证明的是“current managed installation 对其完整 7b2 Admission 作出不可篡改陈述”，而不是 Control Plane 已直接
访问 installation 的 Harness 文件。

## Artifact 与签名域

`EvolutionStablePromotionRuntimeAdmissionSubmission` 冻结：

- 完整 `EvolutionStablePromotionRuntimeObservationAdmission`；
- installation-local Harness attestation 为 true；
- remote Harness direct revalidation 为 false；
- Window、Promoted Outcome、Execution authority 全部为 false；
- `submitted_at`；
- `ReleaseInstallationSignature`。

签名使用独立 domain：

```text
naumi.release.stable-promotion-runtime-admission.v1
```

它不能被 Remote Readiness Probe、Finalization Result 或 Delivery ACK 的签名重放。Submission 与 Receipt 都使用 canonical
JSON SHA-256 内容寻址；外部 Base64 必须 canonical、非空且解码后不超过 64 KiB。

## Durable 发送与接收

installation 侧 `prepare()`：

1. 从 session SQLite 按 Admission ID 重读 exact durable Admission；
2. 调用 7b2 动态 inspect，拒绝 stale Admission；
3. 解析 current Contract、Population Finalization、member 与 Population Credential；
4. 逐字段验证 Admission lineage；
5. 使用 installation key 签名；
6. 在 `BEGIN IMMEDIATE` 内把 signed Submission 写入 outbound table；
7. 同一 Admission 的并发 prepare 收敛到同一 Submission。

Control Plane 侧 `receive()`：

1. 严格解码并验证 Submission 内容身份；
2. 从指定 current Population Snapshot 解析 current Credential；
3. 验证独立 domain 的 Ed25519 signature；
4. 重读 exact durable Contract、Population Finalization 与 member Receipt；
5. 在 `BEGIN IMMEDIATE` 内写入 Delivery Receipt；
6. 同一 exact Submission 的并发或延迟重试返回既有 Receipt，不因新的 wall-clock `received_at` 产生冲突；
7. 同一 Admission 的不同 Submission 失败关闭。

动态 inspect 会重新检查 durable Receipt、Contract、Population member、current Credential 与 signature。Credential 过期、
Population Snapshot 失效、Contract/Finalization 撤权或 durable row 篡改都会撤销
`remote_admission_delivery_authority`。

## 双通道

- Agent Tool：`evolution_stable_promotion_runtime_admission_delivery`；
- Slash：
  - `/evolution stable-promotion-admission-delivery prepare <runtime-admission-id>`；
  - `/evolution stable-promotion-admission-delivery export <runtime-admission-id>`；
  - `/evolution stable-promotion-admission-delivery receive <submission-base64>`；
  - `/evolution stable-promotion-admission-delivery inspect <runtime-admission-id>`；
- New UI、CLI 与 Textual TUI 继续使用同一 Slash Router、Tool Registry 与 Service；
- Moderate/Bypass 均不二次确认；Lockdown 不允许写入。

`export` 只输出可移植的公开签名 artifact，不输出 installation 私钥。当前 Base64 主要用于人工/测试桥接，不能冒充生产传输。

## 验收标准

- [x] current Population Credential 对完整 7b2 Admission 使用独立 domain 真实 Ed25519 签名；
- [x] Submission/Receipt 内容寻址、canonical Base64 与 64 KiB 外部边界；
- [x] installation outbound 与 Control Plane receipt durable Store；
- [x] exact Contract、Population Finalization、member Receipt、Snapshot、Credential 与 signature 复验；
- [x] concurrent prepare、concurrent receive 与 delayed retry 幂等；
- [x] source 缺失、Submission 篡改、Credential 过期和 durable row 篡改失败关闭；
- [x] Agent Tool、Slash、权限、Engine composition 与 public lazy export；
- [x] Window、长期指标、Promoted Outcome、learning、promotion、execution authority 全部保持 false；
- [x] 真实 Release/Stable Deployment/managed runtime/Harness/Admission/Ed25519 场景通过；
- [x] 小模块 ruff、compile、pytest 与 diff check；未运行全量测试。

## 当前边界与下一步

当前 outbound table 只保证 Submission 在 transport 前 durable，不包含 lease claim、远端 endpoint 解析、mTLS、ACK、retry
budget、dead-letter、shutdown drain 或跨进程 Worker supervision。`receive` 是真实 Control Plane 接收边界，但本切片没有把它
挂到网络服务器。

后续 [EVO-05.7b2a2a](EVO-05-7b2a2a-fenced-runtime-admission-delivery-worker.md) 已交付 transport Protocol、本地真实
Control Plane adapter、owner/epoch/lease fencing、Receipt ACK、指数退避、dead-letter 与 Engine lifecycle。下一独立切片为
[EVO-05.7b2a2b](EVO-05-7b2a2b-authenticated-runtime-admission-http-transport.md) 已补齐 mTLS HTTP、证书 pin、严格
网络边界和配置自动装配。下一步进入 `EVO-05.7b3a` 的 Population observation chain cursor；任何单个 signed
Admission 或 Delivery Receipt 都不得被投影为 fleet sustained health 或 `promoted` Outcome。
