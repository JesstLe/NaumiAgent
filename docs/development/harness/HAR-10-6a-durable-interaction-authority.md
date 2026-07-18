# HAR-10.6a Durable Interaction Authority

## 目标

为长周期运行提供单一、跨进程可恢复的用户交互权威状态。问题不能只存在于 Bridge Future 或前端卡片中，
答案也不能依赖“第一个写入者碰巧成功”。本切片在 Harness DB v13 建立 append-only interaction authority，
覆盖 Pursuit、tool、browser、agent 和 runtime 五类 subject。

本切片交付存储与状态迁移核心；Pursuit checkpoint 引用、Bridge 重放和 UI-18.4 页面属于 HAR-10.6b。

## Typed record

`HarnessInteractionRecord` 是 frozen、extra-forbid、strict Pydantic 记录，包含：

- 稳定 `ask-*` interaction ID、subject kind/ID、session 与 agent；
- 2..3 个唯一选项、自定义输入策略、创建时间与可选问题 timeout；
- 当前 owner ID、owner epoch 与 owner lease expiry；
- 单调 sequence、pending/answered/expired/cancelled 状态；
- option/custom 互斥答案、回答者与回答时间；
- canonical JSON 与 SHA-256。

所有持久文本在进入模型前完成控制字符清理和 secret-shaped 内容脱敏。Store 不接受通过直接构造绕过脱敏的
合法模型。问题和答案均有独立长度上限，未回答记录不能夹带答案字段。

## Authority 与事件链

Harness DB v13 新增：

- `harness_interactions`：每个 workspace/interaction 的最新权威快照；
- `harness_interaction_events`：从 sequence 1 开始的完整 transition 哈希链；
- pending subject 索引：按 workspace、state、subject kind/ID 有界读取。

读取时同时验证 event sequence、每事件摘要、previous digest、事件元数据，以及 snapshot 与事件链末端一致性。
篡改任意一侧都会拒绝读取，错误信息不回显 payload。

## 状态与 fencing

- create 只接受 pending/sequence 1；相同 ID+内容幂等，不同内容冲突；
- answer 必须匹配 expected sequence、当前 owner ID、owner epoch 和有效 owner lease；
- option 只能携带选项 value，custom 只能携带非空 custom text，并再次与原问题校验；
- 一个答案事务写 snapshot 与 terminal event，并发双答只能有一个成功；
- takeover 仅允许 pending 状态；新 owner 必须等待旧 owner lease 过期，成功后 epoch 单调增加；
- 同 owner 可续租但仍推进 sequence，防止旧快照覆盖新 lease；
- timeout 是显式 pending→expired 事件；读取 pending 列表不会产生隐藏写入；
- 已到问题 timeout 的记录不能 answer 或 takeover，必须显式 expire。

## 验收证据

- 真实 SQLite 创建后由新的 `HarnessStore` 实例完整恢复；
- 重复 create 不增加事件，不同输入冲突；
- owner/epoch/sequence 错误拒绝 answer；
- owner lease 有效时拒绝 takeover，过期后新 owner 获得 epoch 2，旧 owner 被 fence；
- 两个独立 Store 并发回答只提交一个 answered 事件；
- timeout 前拒绝 expire，到点后产生 sequence 2 并从 pending 查询消失；
- event payload 被篡改后摘要校验拒绝，异常不泄漏正文；
- v1/v2/v3/v4/v8/v11/v12 旧 Harness DB 通过 additive schema v13 初始化且原记录保留；
- 只运行 interaction authority、schema migration 与受版本影响的精确测试节点。

## 当前不足与下一切片

- Bridge 尚未在 request/resolve 时调用 authority，进程重开不会自动重放 pending 问题；
- Pursuit `CheckpointInteraction` 尚未只引用 stable interaction ID，resume 仍停在 `interaction_required`；
- 没有 answer 消费确认、Pursuit answer→checkpoint 的同库原子事务，跨 Harness/Pursuit Store 仍需 reconcile；
- UI-18.4 尚未显示 durable pending/expired/takeover 状态；
- cancelled 已保留为合法终态，但显式 cancel authority 尚未开放；
- 当前正文是脱敏明文而非加密存储；密钥管理与 at-rest encryption 属于 ARC-08/打包安全路线。

下一切片 HAR-10.6b 应把 Bridge/Pursuit 接入本 authority：问题先持久化再展示，答案先 fenced commit 再解除
Future，checkpoint 只保存 interaction ID，resume 从 answered/expired 权威事实继续且不额外消耗模型轮次。
