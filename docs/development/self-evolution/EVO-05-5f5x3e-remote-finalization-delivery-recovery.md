# EVO-05.5f5x3e Remote Finalization Delivery and Recovery

## 目标与边界

x3d 已能生成 signed Execution Grant、在目标安装执行 expected-pointer CAS，并由 installation key 签署结果；但 portable
package 仍依赖人工搬运，也没有权威事实区分“已排队”“目标已收到”和“目标已完成”。本切片补齐 transport-neutral 的 durable
delivery authority、安装端 ACK、重试 fencing、目标 journal，以及 writer 已提交但 keyring 暂时不可用时的受限补签恢复。

本切片不实现具体 HTTP/mTLS daemon，也不把 ACK 冒充执行完成。网络适配器只允许消费本切片的 claim/package 和提交 ACK；
Population member 聚合、缺员策略、配置/数据迁移与 Promotion authority 仍属于后续切片。

## Authority 分层

```text
x3d signed Execution Package
  -> durable Delivery queued event
  -> claim(owner digest, epoch, lease, attempt)
  -> target verifies Grant and writes local journal
  -> installation-signed Delivery ACK (writer_executed=false)
  -> Control Plane acknowledged event
  -> target writer + installation-signed Result
  -> ordinary ingest OR bounded late recovery ingest
  -> Delivery completed bound to x3d Receipt
```

四类事实严格分离：

- Delivery Package 只包装 x3d Execution Package，`execution_authority_expanded=false`；
- Delivery ACK 只证明目标验证 package 并写入 durable journal，固定 `writer_executed=false`、`result_authority=false`；
- Finalization Submission 继续使用 x3d 独立 result signature domain；
- Delivery completed 必须绑定真正的 x3d Receipt，不能由 ACK 单独形成。

## Durable Outbox 与重试

`EvolutionStableRemoteFinalizationDeliveryStore` 与 x3d Grant Store 共享 SQLite，状态机为：

```text
queued(next_attempt_at)
  -> in_flight(owner_sha256, epoch + 1, attempt + 1, lease)
  -> queued(backoff, failure_code)
  -> in_flight(higher epoch after due/expired takeover)
  -> acknowledged(exact installation ACK)
  -> completed(exact x3d Receipt)
```

约束：

- 每个 Grant 至多一个 Delivery；跨时钟重放 queue 返回原记录；
- owner 原文不落库，只保存 SHA-256；
- claim 扫描有界为 1000 条，live lease 不可抢占；
- retry 必须匹配 live owner、epoch 和 lease，持久化指数退避；
- lease 到期 takeover 单调增加 epoch/attempt，旧 owner 被 fencing；
- ACK 使用新 domain `naumi.release.stable-remote-finalization-delivery-ack.v1`，由 current Population Credential 验签；
- ACK/Receipt 冲突失败关闭，精确重放幂等；
- 每次状态转换写 append-only digest chain，读取时复验 sequence、previous digest 与主记录。

当前 outbox 是 transport-neutral authority：daemon 可以 claim 并发送 Base64 package，但 daemon/mTLS adapter 和周期 worker 不在本切片
内。这样不会把尚不存在的网络层描述成已交付。

## Target Execution Journal

目标安装在 `<release-root>/state/stable-finalization-delivery.sqlite3` 保存公开协议 artifact：

- `received`：package 已通过 Trust Policy、双层 control signature、member 与有效期校验，并已形成 installation-signed ACK；
- `writer_committed`：Release Store 已存在 exact finalization；即使随后 keyring 签名失败，异常路径也会机械读取并持久化该事实；
- `result_signed`：writer 完成或既有 writer fact 被恢复，Result 已由 installation key 签署。

journal 不保存私钥、nonce、workspace path 或用户数据；Release Store 与 installation key 必须属于同一 release root。目标必须先
`receive` 才能 `execute`，因此 UI/daemon 无法跳过验证直接写入。

## 受限 late-result recovery

故障窗口：Release Store CAS 已提交，但 installation keyring 在签名时临时不可用；等 keyring 恢复时原 Grant 已过期。

`recover_stable_remote_finalization_submission()` 的不变量：

1. 只读取 `get_stable_member_finalization(authorization_id)`；不存在时拒绝，绝不调用 writer；
2. durable finalization 的 `finalized_at` 必须落在原 Grant `[issued_at, expires_at)`；
3. 用 `finalized_at` 复验原 Authorization 与 Execution Grant signature；
4. 复验 finalization authority、Credential、installation public key 与 release root exact binding；
5. 只重新构造同一 deterministic Result 并补签。

Control Plane 的 `ingest_late_recovery()` 只在原 source/probe/control/consumption 仍一致、current Trust Policy 仍能验证原 package、
writer completion 落在原窗口、补签不晚于当前时间且距离 Grant expiry 不超过 24 小时时接受。kill switch generation、Probe、Credential
或 Trust Policy 任一变化仍失败关闭。普通 `ingest()` 不放宽，过期后继续明确拒绝。

late Receipt 在动态 inspect 中显示为历史完成，而不是 current execution authority；它仍不形成 Population completion 或 promotion。

## 双通道

Agent Tool 继续复用 `evolution_stable_remote_finalization`，新增 actions；Slash Router、CLI、TUI 与 New UI 共用同一入口：

- `queue-delivery <grant-id>`
- `claim-delivery <owner-id>`
- `retry-delivery <delivery-id> <owner-id> <claim-epoch> <failure-code>`
- `receive-delivery-local <delivery-package-base64>`
- `ack-delivery <delivery-id> <ack-base64>`
- `execute-delivery-local <delivery-package-base64>`
- `recover-delivery-local <delivery-package-base64>`
- `ingest-delivery <delivery-id> <submission-base64>`
- `ingest-delivery-late <delivery-id> <submission-base64>`
- `inspect-delivery <delivery-id>`

所有现有允许模式均不触发二次确认；lockdown 继续由原工具 Permission Rule 阻断，bypass 保持全权限直通。

## 验收标准

- [x] 真实 x3d Grant 跨时间重复 queue 收敛为一个 Delivery；
- [x] 12 路跨 Store claim 只有一个 owner 成功；
- [x] retry 在 due 前不可重领，expired takeover 增加 epoch，旧 owner 被 fencing；
- [x] Delivery Package 和 ACK strict Base64/JSON round-trip；
- [x] 目标先验证 package、写 journal，再以独立 domain 签 ACK；ACK 明确不代表 writer/result；
- [x] 真实 Release Store writer、Result ingest 与 Delivery completed 闭环；
- [x] 模拟 writer 后 keyring 故障，过期后只补签既有 finalization 并形成 late Receipt；
- [x] 无 durable writer fact 时 recovery 拒绝，不执行 CAS；
- [x] Agent Tool 与 Slash 走同一 Service/Store/Journal；
- [x] Engine、public lazy exports、帮助文案与权限语义同源；
- [x] 只运行相关小模块测试、Ruff、compile 与 public API smoke，不运行全量测试。

## 自我审视与下一步

本切片真实关闭了“Control Plane 不知道包是否收到”和“writer 已提交但签名丢失”的两个 durable gap，但仍有明确边界：

- 未实现 daemon/mTLS 网络 adapter、周期 worker、shutdown drain、dead-letter、人工 requeue 与 retention；
- Target Journal 依赖本机 SQLite 事务，尚未做 kill-at-every-write-point 和 Windows forced-kill 矩阵；
- outbox event chain 可检测普通篡改，但没有像 Harness receipt chain 那样增加本地 HMAC；
- 24 小时 late window 当前是协议常量，后续需纳入 signed policy/config，而不是 UI 自定义；
- 尚未用 fresh remote Probe 把 Receipt 的 `remote_active_pointer_current_unverified` 更新为当前事实。

下一最小切片应比较 Harness delivery worker、Supervisor/heartbeat 与 Population aggregation 的依赖，优先补
**EVO-05.5f5x3f Remote Finalization Delivery Worker**：只把现有 outbox 接到 authenticated installation transport，加入有界周期
claim、ACK timeout、retry budget、dead-letter 与 shutdown drain。完成真实自动 transport 后，才进入 Population member Receipt 聚合。
