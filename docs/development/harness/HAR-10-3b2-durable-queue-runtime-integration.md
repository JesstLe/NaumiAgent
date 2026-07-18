# HAR-10.3b2 持久队列 Runtime 接入

## 目标

把 HAR-10.3b1 的持久 Store 接入真实 New UI Bridge，使尚未派发的排队消息在进程崩溃后不会丢失，并在用户
显式恢复原会话时继续执行。派发边界必须由现有 HAR-10.1 RunLease fencing 保护；不能因“恢复队列”自动重放
一条可能已经触发工具副作用的消息。

## 依赖选择

本切片复用 `HarnessRunKind.RUNTIME` 的 lease/epoch，不创建第二张 claim 权威表，也不等待 ARC-02 服务化。
现有 `run/queued`、`run/queue_promoted`、`run/completed` 和错误事件已经能表达用户闭环，因此不新增 ARC-03
协议事件。TUI 当前在模型运行期间禁用输入，尚不存在可持久化的并发对话队列；本切片提供共享 Python authority，
但不把 TUI 标记为已具备该交互能力。

## 运行状态机

1. 首条正在运行的消息保持现有路径；后续消息必须先成功写入 v14 Store，Bridge 才发送 `run/queued`。
   新 payload 摘要只绑定 session/request/text，临时 client id 不破坏跨 Bridge 幂等；HAR-10.3b1 已写入的
   client-bound v14 摘要保持兼容读取。
2. 每个 queue item 的 claim run id 由 workspace/session/request id 确定性派生；领取前若已存在任何历史 lease，
   自动派发 fail closed，要求核对上次运行。
3. 领取后 Bridge 每 `lease_seconds / 3` 续租；续租失败立即取消当前模型运行，不再提交终态或推进后续队列。
4. completed/failed/cancelled 与 lease release 在一个 SQLite `BEGIN IMMEDIATE` 事务中提交；owner/epoch/expiry
   任一不匹配均拒绝终态，并写入 accepted fence audit 后才释放正确 claim。
5. `completion/receipt`、Harness receipt 和 `run/completed` 对 durable item 延迟到 fenced terminal commit 之后；
   用户不会先看到完成、随后才发现队列结果未持久化。
6. 正常 `/q` 会把尚未 claim 的排队项持久化为 cancelled；进程崩溃不会执行这段清理，因此安全等待项仍可恢复。

## 恢复语义

- 新启动默认仍是全新 UI 状态，不自动恢复旧会话或旧输入；
- 用户显式 `/resume`，或启动参数已经显式预载 Session 时，Bridge 才读取该 session 的 durable queue；
- 从队首开始，所有从未出现 claim 的连续前缀可恢复、重新显示并继续执行；
- 一旦遇到 active/expired/released 历史 claim，该项及其后缀停止自动派发，显示
  `queue_claim_ambiguous`/`queue_claim_released`，防止越过未知副作用边界；
- 重复 resume 不重复插入本地队列或用户消息。

## 验收证据

- 模拟进程崩溃保留未领取消息；新 Bridge 显式恢复同一 Session 后自动 claim、执行、fenced terminal 并清空队列；
- 模拟“已 claim 后崩溃”时，新 Bridge 不调用模型，保留消息并输出恢复阻断；
- 两个 authority 并发领取同一 item 只有一个成功；过期/错误 epoch 不能终结或释放记录；
- durable promote 在 Store 与内存中顺序一致，正常 shutdown 后未领取项不再恢复；
- completion receipt 发送时机械观察到 queue item 已经离开 queued 状态，receipt 仍早于 correlated run completion；
- 原有内存 fallback 的入队、20 条容量、提升、失败推进、取消和 shutdown 定向用例无回归。

## 当前不足与后续

- HAR-10.3b3 已补齐 `/queue list` 证据审查、live-owner 拒绝、过期/released claim 的显式重试或放弃，
  以及 New UI 处置后立即刷新；见 [HAR-10.3b3](HAR-10-3b3-ambiguous-queue-resolution.md)。
- terminal 记录尚无 retention/cursor；跨客户端轮转公平和优先级仍未实现。
- TUI fallback 需要先独立实现“运行中仍可输入并显示队列”的 UI 能力，再接同一个 authority；不能仅在后台接 Store。
- 本切片保证未派发等待项的 crash recovery，不宣称正在运行的模型/工具 exactly-once；外部副作用仍由各执行域的
  HAR-10.5 idempotency/reconcile 负责。
