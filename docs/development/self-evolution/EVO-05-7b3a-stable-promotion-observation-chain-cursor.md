# EVO-05.7b3a Stable Promotion Observation Chain Cursor

## 目标与依赖

[EVO-05.7b2a2b](EVO-05-7b2a2b-authenticated-runtime-admission-http-transport.md) 已让 installation 的 signed Runtime
Admission 经 mTLS 到达 Control Plane，并把 exact Receipt ACK 持久化回 installation。7b3a 在这个 ACK 之后，从 installation
本地 [HAR-10.2j](../harness/HAR-10-2j-runtime-release-observation-ledger.md) 读取真实 heartbeat observation chain，建立可恢复、
content-addressed 的 durable cursor。

本切片不接受调用方提供的 sample，不把 HAR payload 视为已经远端认证，也不计算 duration、gap、coverage、Population sustained
health 或 promoted Outcome。它只形成下一步 signed revision delivery 的本地输入。

## 为什么不是“可变页 + 一个整数”

如果 cursor 直接按“本次读到的 1..500 条”形成一个 page artifact，并发 worker 可能在相同 `after_sequence` 读到不同长度：一个
读到 sequence 1..10，另一个在新 heartbeat 到达后读到 1..11，由此产生两个合法但互斥的 page identity。

7b3a 因此采用两层结构：

- HAR 每次仍有界读取最多 500 条；
- 每一个 observation 都形成一个确定性的 `EvolutionStablePromotionObservationChainRevision`；
- revision identity 绑定 exact sample、Admission、Control Plane Receipt、Binding、member、contract 与前一 revision SHA；
- `EvolutionStablePromotionObservationChainCursor` 只指向当前最后 revision，`after_sequence` 是恢复位置；
- 两个进程读取到重叠批次时，SQLite `BEGIN IMMEDIATE` 会逐 sequence 比对既有 revision，相同内容幂等，任何分叉 conflict。

这样批量 IO 与单 sample 确定性可以同时成立。cursor 最多 5000 个 revision，与 7b1 的 `maximum_sample_count` 一致；一次读取最多
500 条，与 HAR ledger page 上限一致。

## 启动前置与 exact lineage

`advance(admission_id)` 必须机械证明：

1. 7b2a2a Dispatch 已处于 `acknowledged`，且 Receipt exact 匹配 Submission；
2. 7b2a1 Delivery Service 重新验证 Receipt 的签名、Population Credential、Observation Contract、member 与 Admission，证明远端
   Admission authority 当前仍有效，而不只依赖历史 ACK 状态；
3. Submission 中的 Admission 仍由 7b2 Service 动态投影为 current observation input；
4. Observation Contract ID/SHA 与 `window_not_before_at` 仍 exact；
5. HAR page 的 workspace、Binding、Runtime Identity、surface、subject、instance、epoch 与 Admission 全部一致；
6. chain origin 是 sequence 1 `startup/starting`，ID/SHA/time 与 Admission 冻结的 origin exact；
7. 后续 sample sequence、sample previous SHA、revision previous SHA 和 observed time 单调连续；
8. phase 只能是 `starting/running/waiting/failed/draining/stopped`，且 `starting` 只能作为 origin；
9. 所有 sample 不早于 Contract 的 `window_not_before_at`。

未 ACK Admission、legacy origin、跨 Binding/runtime、缺页、重复 sequence 不同内容、hash/time 断裂、超过 5000 条或 durable
source 损坏全部失败关闭。

## Store、恢复与动态重验

Cursor Store 与 Admission/Dispatch 共用 session SQLite：

- revision 表以 `revision_id` 为主键，并对 `(admission_id, heartbeat_sequence)` 唯一；
- latest cursor 表按 Admission 唯一，只保存 content-addressed head；
- 写入事务内重读 durable Admission、Submission、acknowledged Dispatch Event 与 Receipt；
- 重复或并发 advance 逐 revision 对账，不覆盖不同内容；
- `get()` 重建全部 revision 并验证数量、head、Admission/Receipt binding、sample hash chain 与 revision hash chain；
- `inspect()` 再次读取 Dispatch，并经 7b2a1 Delivery Service 重验远端 Receipt/Credential/Contract/member authority，再重验本地
  Admission/Contract authority、Runtime Binding 与 HAR exact sample slice；
- HAR retention、Binding 漂移、Admission/Contract 撤权或 revision tamper 会令 Cursor stale 或直接报 durable corruption。

Cursor JSON 上限 2 MiB，单 revision 上限 512 KiB；revision 只保存 HAR typed observation，不保存 API key、环境变量、argv、用户
消息、模型输出、工具参数或证书私钥。

## 双通道与用户体验

- Agent Tool：`evolution_stable_promotion_observation_chain_cursor(action, admission_id)`；
- Slash：`/evolution stable-promotion-observation-chain advance|inspect <runtime-admission-id>`；
- New UI、Textual TUI 与 legacy fallback 继续经过同一 Slash/Tool/Service；
- 回执分别显示历史 ACK、当前 Remote Admission authority、HAR chain current、sample 进度、最新 phase 和 Cursor delivery input；
- 这是本地治理 artifact，Moderate/Bypass 均不二次确认。

## Authority 边界

只有 durable/local sources 与远端 Admission authority 全部 current 时，View 才设置
`cursor_delivery_input_authority=true`。以下字段始终为 false：

- `signed_page_delivery_authority`；
- `observation_window_authority`；
- `long_term_metrics_authority`；
- `population_observation_authority`；
- `promoted_outcome_authority`；
- `learning_authority`、`promotion_authority`、`execution_authority`。

## 验收标准

- [x] 未收到 Control Plane Receipt ACK 时不能创建 Cursor；
- [x] 已 ACK Receipt 仍经 Delivery Service 重验当前 Credential/Contract/member/signature authority；
- [x] 真实 Stable release → managed runtime → HAR startup/running chain 被读取并形成 sequence 1–2 revisions；
- [x] 新 heartbeat 后两个独立 Service 并发 advance 收敛到同一 sequence 3 Cursor；
- [x] 无新 sample 时重复 advance 幂等；
- [x] revision sample hash 与 revision SHA 双链连续；
- [x] nested authority 提升被 `Literal[False]`/content validator 拒绝；
- [x] durable revision tamper 被 chain corruption 检出；
- [x] Tool、Slash、Moderate/Bypass permission 与中文回执共用同一 Service；
- [x] Engine composition、public lazy API、Ruff、compile、diff/YAML 与定向 pytest 通过；
- [x] 按用户要求未运行全量测试。

## 当前不足与下一步

7b3a 的 HAR samples 仍只在 installation 本地可信。即使 Cursor ready，也不能声称 Control Plane 已直接验证 heartbeat，更不能跨
installation 聚合。[EVO-05.7b3b1](EVO-05-7b3b1-signed-observation-revision-delivery.md) 已用 current Population Credential 把
Cursor revisions 形成有界 signed batch，并由本地真实 Control Plane boundary 逐 revision 验签、顺序幂等接收；
[EVO-05.7b3b2a](EVO-05-7b3b2a-fenced-observation-revision-delivery-worker.md) 已补齐自动 fenced Worker，
[EVO-05.7b3b2b](EVO-05-7b3b2b-authenticated-observation-revision-http-transport.md) 已补齐 mTLS 网络交付。下一步 7b3c
补齐 durable fenced worker、Receipt ACK 与 mTLS transport；完成跨安装投递后，7b3c 才能按 7b1 的 duration、Population denominator/member coverage、
sample/gap/latest-age 规则形成
Population long-term assessment。
