# EVO-05.7b3b2a Fenced Observation Revision Delivery Worker

## 目标与依赖

[EVO-05.7b3b1](EVO-05-7b3b1-signed-observation-revision-delivery.md) 已完成 bounded signed revision batch 与 Control Plane
顺序验签/Receipt，但仍依赖调用方手动 prepare、transport submit 和 Receipt 收口。7b3b2a 增加 durable automatic dispatch：每个网络
attempt 必须先获得 owner/epoch/lease claim，Receipt 只能由仍持有 live claim 的 Worker ACK；失败按有界指数退避重试或进入
dead-letter。

本切片使用真实 in-process Control Plane adapter 验证完整 sender→receiver 边界，不实现跨主机 mTLS。下一切片 7b3b2b 只替换
Transport Port，不改变 Store、Worker、Tool 或 Receipt 语义。

## Dispatch 状态机

每个 signed Submission 拥有独立 append-only event chain：

```text
queued -> in_flight -> acknowledged
                    -> queued       (retry)
                    -> dead_letter
in_flight(expired) -> in_flight     (new owner/epoch)
```

Event 冻结 Submission/Admission/range identity、monotonic event sequence、owner SHA-256、claim epoch、attempt count、lease、next
attempt、Receipt、failure code、occurred time 和 previous event SHA。owner 原文不持久化。

只有 `in_flight` 可携带 owner/lease；只有 `acknowledged` 可携带 Receipt；只有 retry queued/dead-letter 可携带 failure code。所有 Event
content-addressed，任何 row、Event JSON、previous SHA、计数或非法状态转换都会令 dispatch chain corruption。

## Local ACK head 与批次衔接

`enqueue_next(admission_id)` 不接受调用方指定 sequence：

1. Dispatch Store 重放该 Admission 的全部 acknowledged batch；
2. 从 `(0, "")` 开始验证每批 prior head、first sequence、Receipt 与 revision SHA；
3. 得到 authoritative local ACK head；
4. 调用 7b3b1 `prepare(after_sequence=head)`；
5. 在事务内验证 exact signed outbox，且同一 Admission 没有 active queued/in-flight batch；
6. 写入 queued Event 并唤醒 Worker。

Worker ACK 一批后会立即尝试 enqueue next batch；没有新 sample 是正常终态，不记为失败。每轮还会有界扫描“最新 Dispatch 已 ACK
且没有后继批次”的 Admission，因此 ACK 后瞬时排队失败或稍后才产生的新 sample 会在后续轮次恢复，不会错误回滚已经确认的批次。
若 64 KiB 边界把一个 Cursor page 切成多个 signed batch，同一 `run_once()` 可在 `scan_limit` 内连续 claim/ACK 后续批次。

dead-letter 不会被自动跳过后签发更高 sequence，避免把永久失败制造成远端 chain 空洞。人工签名 requeue/abandon 是后续治理切片。

## Fencing、重试与关闭

- claim lease：3..300 秒；过期 claim 可由新 owner 获取更高 epoch；
- retry：`min(max, base * 2^(attempt-1))`，next-attempt 前不可 claim；
- timeout、连接/OSError 默认 retryable；typed lineage/signature/head conflict 为 permanent；
- 达到 `max_attempts` 或 permanent failure 进入 dead-letter；
- Receipt 必须 exact 匹配 Submission，且 `received_at <= ACK occurred_at`；
- 旧 owner、旧 epoch、已过期 lease 的 ACK/retry/dead-letter 全部 fenced；
- Worker 具有 bounded scan、空轮/失败 backoff、jitter、wake、单实例 run lock 与 shutdown drain；
- shutdown 超时会取消 task 并计入 `forced_shutdown_count`，未 ACK claim 由 lease expiry 恢复。

## Engine、配置与双通道

Harness 配置新增 `stable_promotion_observation_revision_delivery`，独立控制 interval、backoff、lease、scan limit、Receipt timeout、retry
budget、shutdown drain 和 jitter。Runtime Services 新增 typed Control Plane Transport Port；Engine：

- 始终组合 Dispatch Store，但只有显式注入 typed authenticated Transport 时才组合 Worker；
- 默认不会把同进程 adapter 冒充生产 Control Plane；Local adapter 只用于真实测试或显式单进程部署；
- long-running startup 先执行一次 recovery pass，再启动周期循环；
- shutdown 在其他资源释放前 drain Worker；
- 暴露 enqueue/run-once/snapshot facade，Tool 不直接操作 Worker 内部状态。

Agent Tool 与 Slash 继续复用 7b3b1 同一个工具：

- `queue <admission-id>`；
- `inspect-dispatch <admission-id|submission-id>`；
- `run-worker`；
- `inspect-worker`。

New UI、Textual TUI 与 fallback CLI 共享 Slash Router；Moderate/Bypass 不二次确认。

## Authority 边界

Dispatch/ACK 只证明 signed revisions 已经由当前 transport 收口。以下 authority 始终为 false：

- `observation_window_authority`；
- `long_term_metrics_authority`；
- `population_observation_authority`；
- `promoted_outcome_authority`；
- `learning_authority`、`promotion_authority`、`execution_authority`。

## 验收标准

- [x] exact 7b3b1 signed outbox 才能 enqueue；
- [x] owner SHA/epoch/lease fencing 与过期 claim 抢占；
- [x] retry next-attempt、max-attempt/permanent dead-letter；
- [x] Receipt exact binding 与 stale owner ACK/retry 拒绝；
- [x] local acknowledged head 从 0 开始跨 batch 连续重放；
- [x] ACK 后自动尝试下一有界 batch，并有界恢复无后继 ACK head；
- [x] append-only event chain、row identity 与 tamper 检测；
- [x] Worker start/wake/run-once/stop/drain 与 snapshot；
- [x] Local Control Plane adapter 真实调用 7b3b1 receive；
- [x] Tool、Slash、Engine config/lifecycle 与 public API；
- [x] Ruff、compile、YAML/diff 与真实小模块 pytest 通过；按用户要求未运行全量测试。

## 当前不足与下一步

- 生产组合根当前不提供默认 Transport；同进程真实 adapter 仅用于测试或显式注入，不证明跨主机 TLS；
- dead-letter 尚无人工签名 requeue/abandon 与 retention；
- Control Plane remote head 与 installation local ACK head 的主动 reconcile 尚未实现；
- Windows/Linux 发布矩阵仍需在网络 transport 切片补证。

下一独立切片 `EVO-05.7b3b2b Authenticated Observation Revision HTTP Transport` 应复用 bounded TLS HTTP common，交付固定
endpoint/media type、mTLS、member certificate pin、current/next server pin、strict request/response limits、timeout、并发和 rate limit。
只有跨安装 signed revision ledger 闭合后，7b3c 才能计算 Population long-term assessment。
