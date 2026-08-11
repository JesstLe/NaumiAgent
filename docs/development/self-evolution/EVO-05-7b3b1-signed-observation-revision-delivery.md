# EVO-05.7b3b1 Signed Observation Revision Delivery

## 目标与依赖

[EVO-05.7b3a](EVO-05-7b3a-stable-promotion-observation-chain-cursor.md) 已从 installation 本地 HAR-10.2j ledger
建立逐 sample、content-addressed 的可恢复 Cursor，但 Cursor 与 HAR sample 仍不是 Control Plane evidence。7b3b1 增加独立
Ed25519 协议域，把 exact Cursor revisions 编码成有界 signed batch，并由 Control Plane 按 exact Runtime Admission Receipt、current
Population Credential、installation member 与 revision hash chain 验证后持久化 Receipt。

本切片不启动后台 worker、不发起网络请求、不计算 duration/gap/coverage，也不形成 Population sustained-health 或 promoted Outcome。

## 签名批次

`EvolutionStablePromotionObservationRevisionSubmissionPayload` 冻结：

- exact Runtime Admission ID/SHA 与已接收 Admission Delivery Receipt ID/SHA；
- 生成批次时的 Cursor ID/SHA；
- `prior_remote_head_sequence` 与 prior revision SHA；
- 连续的 `first_sequence..last_sequence` 和 1..500 个完整 typed Cursor revisions；
- 由 Admission Receipt 与当前 Cursor head observation 取最大值得到的 deterministic `submitted_at`，实际签名时间保存在
  signature `signed_at`，并冻结全部 authority=false 边界。

批次必须从 `prior_remote_head_sequence + 1` 开始，每条 revision 的 Admission、Receipt、sequence 和 previous revision SHA 必须
连续。调用方只能指定恢复位置，不能提交 sample、revision 内容、Credential、member 或 Cursor identity。

签名域固定为：

`naumi.release.stable-promotion-observation-revisions.v1`

它与 readiness、finalization、delivery ACK 和 Runtime Admission 域隔离。canonical payload 不得超过 64 KiB；Service 在最多 500
条的逻辑页内逐条扩展，选择真实可签名的最大前缀。单 revision 已超过上限时失败关闭，不截断字段或改签摘要占位物。

## Control Plane 顺序接收

Control Plane receive 必须重新证明：

1. exact Runtime Admission Submission/Receipt 仍持久化且动态 authority 为 current；
2. Population Snapshot 当前有效，Credential 与 Admission、member 和 installation public key exact；
3. Ed25519 signature 在独立 revision domain 下验证通过；
4. Submission 前置 head 与 Control Plane durable head 的 sequence/SHA exact；
5. batch 内每条 revision 的 typed identity、Admission/Receipt binding、sequence 与双 hash chain 连续；
6. `received_at >= signed_at >= submitted_at >= latest observed_at`；
7. Receipt 与 Submission/signature/Credential/range/latest revision SHA exact。

首批必须以 `(0, "")` 为前置 head。后续只接受 exact next batch；跳号、重叠的不同 batch、旧 head 分叉、不同 Admission Receipt 或
不同 Credential 全部失败关闭。同一 Submission 并发或重试接收返回同一 Receipt，不因 head 已前进而误报 conflict。

## Store、恢复和撤权

Store 与 Cursor、Runtime Admission Delivery 共用 session SQLite，包含：

- append-only signed batch outbox，以 Submission ID 为主键、`(admission_id, first_sequence)` 唯一；
- append-only remote Receipt，以 Receipt ID 为主键、Submission 唯一；
- 每个 Admission 一个单调 remote head，保存 last sequence、revision SHA 与 Receipt identity。

所有写入使用 `BEGIN IMMEDIATE`。outbox 在事务内重读 exact Cursor/revision JSON；receiver 在事务内重读 exact Admission Receipt 与
remote head。`inspect()` 重放全部 received batches，验证每批前置 sequence/SHA、Receipt 与最终 head，而不只比较 head 数字。
durable row 与 JSON identity 不一致、artifact 超过 2 MiB、head/receipt tamper 均失败关闭。

`inspect()` 动态重验 Admission Delivery、Population Credential、签名和 remote head。Credential 到期或撤销后，历史 Receipt 仍是
不可变事实，但 `remote_revision_delivery_authority=false`。installation Cursor 是否仍保留只作为诊断字段；Control Plane 已验签的
remote evidence 不依赖 installation retention 才存在。

## 双通道

- Agent Tool：`evolution_stable_promotion_observation_revision_delivery`；
- Slash：`/evolution stable-promotion-observation-revisions prepare <admission-id> [after-sequence]`；
- `export <submission-id>`、`receive <payload-base64>`、`inspect <receipt-id>`；
- New UI、Textual TUI 与 fallback CLI 继续共享 Slash Router、Tool Registry 与同一 Service；
- Moderate/Bypass 不二次确认；任何模式都不能提升 authority。

## Authority 边界

只有 durable Receipt、current Admission Delivery、current Credential、valid signature 和 remote chain head 全部成立时，View 才设置
`remote_revision_delivery_authority=true`。以下字段始终为 false：

- `observation_window_authority`；
- `long_term_metrics_authority`；
- `population_observation_authority`；
- `promoted_outcome_authority`；
- `learning_authority`、`promotion_authority`、`execution_authority`。

## 验收标准

- [x] 独立 Ed25519 domain，签名与 current Population Credential 验证；
- [x] canonical payload 64 KiB、逻辑 batch 500 revisions、artifact 2 MiB 三层边界；
- [x] exact Cursor/Admission Receipt source、sequence 和 revision SHA chain；
- [x] 两个 Service 并发 prepare/receive 幂等收敛；
- [x] 第二批 exact head 前进，跳号/旧 head batch 失败关闭；
- [x] Credential 到期动态撤权，Receipt fact 保留；
- [x] Receipt tamper 与非 canonical Base64 失败关闭；
- [x] Tool、Slash、Engine、Moderate/Bypass 与 public lazy API；
- [x] Ruff、compile、diff/YAML 与真实小模块 pytest 通过；按用户要求未运行全量测试。

## 当前不足与下一步

7b3b1 只提供可由 adapter 调用的本地真实 Control Plane boundary；后续
[EVO-05.7b3b2a](EVO-05-7b3b2a-fenced-observation-revision-delivery-worker.md) 已完成 durable owner/epoch/lease Worker、
自动 next-batch chaining、Receipt ACK、retry/dead-letter 与 Engine lifecycle。当前仍未跨主机传输。下一步：

1. `EVO-05.7b3b2b` 复用 bounded TLS HTTP common、mTLS certificate pin、strict media type/size/timeout/rate limit；
2. `EVO-05.7b3c` 只读取远端已验签 revision ledger，按 7b1 contract 计算 installation verdict 与 Population coverage。

任何本地 Cursor、signed batch 或单 member Receipt 都不得被投影为 Population sustained health 或 promoted Outcome。
