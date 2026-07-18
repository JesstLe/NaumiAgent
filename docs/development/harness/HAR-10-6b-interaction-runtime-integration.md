# HAR-10.6b Durable Interaction Runtime Integration

## 目标

把 HAR-10.6a 的 interaction authority 接入真实 New UI Bridge 与 Pursuit checkpoint。模型发起的问题必须
先成为可恢复事实再显示；用户答案必须先通过 owner/epoch/sequence fencing 提交，再释放等待中的工具。
Pursuit checkpoint 只保存稳定 `ask-*` 引用，不重复保存问题正文或猜测答案。

本切片是 HAR-10.6 的运行时垂直切片，不扩张成 Goal 页面动作、TUI takeover 页面或通用 durable queue。

## 运行协议

### 实时问题

1. `AgentEngine.request_user_input()` 为每次调用生成稳定 interaction ID，并通过 task-local
   `ContextVar` 识别当前 PursuitRun；并发任务不会共享或覆盖运行上下文。
2. Bridge 规范化问题并在 Harness authority 创建 pending 记录。创建失败时不显示不可恢复卡片。
3. 当前调用属于 Pursuit 时，Pursuit 在持有有效 run lease 的边界写入
   `CheckpointInteractionRef(authority="harness", interaction_id=...)`，随后 Bridge 才发送
   `interaction/request`。
4. timeout 作为整数秒进入领域模型、authority `expires_at` 与 Node 严格协议；合法范围为
   3..604800 秒，省略表示不自动超时。
5. Bridge 为 live/replayed pending 问题安排独立 deadline task；到期时 fenced 提交 expired、解除等待并发送
   `interaction/resolved(status=expired)`，New UI 卡片转为“已超时”并推进下一排队问题。

### 回答提交

1. Bridge 先用原始 pending request 校验 option/custom 答案。
2. Harness Store 按 expected sequence、owner ID 和 owner epoch 原子提交 answered event。
3. Pursuit 在 run lease fence 下写入 hard interaction evidence、清除 checkpoint 引用。
4. 只有以上提交成功，Bridge 才解除 Future 并发送 `interaction/resolved`。

如果 authority 已提交但 Pursuit checkpoint 写入失败，Bridge 不伪装成功，而是提示用户执行
`/pursue resume`。恢复器会从 answered authority 幂等补写 evidence 和无 pending 引用的新 checkpoint。

## 重启恢复

- Bridge 启动 ready 后有界读取最多 50 个 pending interaction；
- 问题 timeout 已到时显式写入 expired transition，不重放过期卡片；
- 旧 owner lease 仍有效时不抢占，并按最早到期时间安排后台复查；
- lease 过期后新 Bridge takeover，owner epoch 单调推进，再发送同一个 interaction ID；
- replay-only Future 在界面关闭时取消，不制造未消费异常；回答仍先写 authority；
- Pursuit resume 对 pending/expired/cancelled 都不调用模型；answered 会先补证据并清除引用，然后才允许继续。

## 跨 Store 故障语义

Harness interaction authority 与 PursuitStore 目前不是同一个事务域。本切片通过顺序和恢复规则收敛，而不宣称
伪原子性：

- create 已成功、checkpoint 失败：保留 authority pending，重开可见，但 subject 需要人工核对；
- answer 已成功、checkpoint 失败：authority 是答案真相，`/pursue resume` 幂等消费；
- checkpoint 引用存在、authority 缺失或 subject 不匹配：fail closed，进入 `interaction_required`；
- legacy checkpoint 保存完整问题而没有 stable ref：保持兼容读取，但禁止消耗新模型轮次。

## 验收证据

- Bridge callback 观察到 request 展示前 authority 已是 pending，Future 释放前 authority 已是 answered；
- 过期 foreign owner 被 takeover 为 epoch 2，同 interaction ID 重放并可回答；
- live foreign owner 不被抢占，Bridge 安排租约到期复查；
- 已超时问题显式 expired 且不产生 UI request；
- Pursuit pending、expired resume 均不调用 assessment model；
- Pursuit answered resume 写入 hard evidence、清除 checkpoint ref，再进入后续预算门；
- Python interaction/Harness/Pursuit/Bridge/Composition 定向子集 37 项通过；
- Node protocol/state/component interaction 子集 7 项通过，93 个 JS 文件语法检查通过；
- 未运行全量测试。

## 当前不足与下一步

- Textual TUI 仍提供实时结构化选项，但尚未复用 durable create/answer/replay coordinator；UI-18.4 因而只达到
  New UI 运行时部分完成，不能标记 parity；
- replay 回答后不会擅自启动一个新 Pursuit 执行器，用户需显式 `/pursue resume`，避免隐藏并发 owner；
- cancelled authority 尚无显式用户动作；
- pending 列表目前上限 50，无 cursor 与优先级；这应与 HAR-10.3 durable queue 一起设计；
- 跨 Store 原子提交、at-rest encryption 和多实例通知仍分别属于 ARC-05/08、ARC-08 与 ARC-06。

下一最小切片应跨文档比较 `UI-18.4b TUI durable parity` 与 `HAR-10.3a immediate-message queue`，选择能直接
改善用户闭环且不提前实现完整 ARC-06 的前置。
