# HAR-10.8f2f Pursuit 终态 Outbox 精确死信重入队

## 目标与范围

HAR-10.8f2e 已把 active dead letter 投影为稳定、脱敏的 `ptfail_...` 审查目标。本切片只交付一种人工处置：
对用户明确选中的最新 active dead letter 执行 exact requeue。动作必须经过统一 ToolExecution 权限链，保留全部
旧失败证据，追加不可变处置回执，并立即恢复自动 worker 的领取资格。

本切片不实现 accept/abandon、删除、历史清理或 retention apply。requeue 也不等同于“忽略失败”：原事件和
dead-letter disposition 永久保留，新失败从新的预算段重新计数。

## 权威模型

### 公开目标

- 输入只能是审查目录提供的 `ptfail_[0-9a-f]{24}`；
- Store 在 `BEGIN IMMEDIATE` 内重新定位 failure event，并复验它仍是当前 failure chain 尾部；
- outbox 必须仍为 pending，dispatch 必须为 idle，failure head 必须仍指向该事件且 `dead_letter=1`；
- 目标不存在、已被处置、不是链尾、head 漂移或时间倒退时均失败关闭。

UI 不接收 outbox ID、attempt/run ID、owner、claim epoch 或内部摘要，因此不能把另一个运行误当成目标。

### 不可变 requeue receipt

`PursuitTerminalDeadLetterRequeueReceipt` 是 frozen schema v1，包含：

- 内容寻址的 `ptreq_...` receipt ID；
- 精确 dead-letter ID；
- source request、旧 failure、处置前后 dispatch 的 SHA-256 绑定；
- 被处置的全局 failure sequence；
- 完全相等的 `requeued_at` 与 `next_attempt_at`；
- 对完整 receipt facts 的 SHA-256。

数据库表 `pursuit_terminal_outbox_dead_letter_requeues` 对 receipt ID、dead-letter ID、source request digest
分别唯一，并以 `(outbox_id, failure_sequence)` 外键绑定原 failure event。相同目标的重放返回首次回执；同一
source request 试图绑定另一个目标时冲突关闭。

### 原子状态转换

一次成功 requeue 在同一事务中完成：

1. 验证完整 failure hash chain、dispatch event chain 和独立 failure head；
2. 构造下一条 idle dispatch event，sequence 加一，清除 `last_failure_code`；
3. 把 `next_attempt_at` 设置为 requeue 时间，立即变为 due；
4. 插入 immutable receipt；
5. 条件更新 failure head 的 active `dead_letter` 位为 false；
6. 追加并持久化新的 dispatch event。

任一步失败都会回滚。不存在“head 已解除但 dispatch 未开放”或“dispatch 已开放但没有处置回执”的中间状态。

## 失败链与预算段

failure sequence 始终全局单调递增，不在 requeue 时清零。新的自动失败预算按最近一条 requeue receipt 的
`failure_sequence` 计算：

```text
segment_attempt = global_failure_sequence - latest_requeued_failure_sequence
```

因此旧段可能是 sequence 1 的永久失败；第一次 requeue 后，sequence 2 是新段第 1 次，sequence 3 是新段
第 2 次。若 sequence 3 再次成为 dead letter，它可以生成第二张独立 receipt；下一段从 sequence 4 重新计数。

验证器允许 dead-letter event 后继续追加 failure 的唯一条件，是存在一张准确连接该 failure 与 requeue 后
dispatch event 的认证 receipt。receipt、前后 dispatch、failure digest、时间或序号任一被篡改，claim、catalog
和 receipt read 都失败关闭。

## Tool、权限与三端入口

Agent Tool `pursuit_terminal_dead_letter_requeue` 是唯一写入口。CLI、Textual TUI 和 New UI 都经
`AgentEngine.execute_tool()`，不得直接调用 Store：

- CLI/TUI fallback：`/pursue outbox requeue <ptfail_...>`；
- New UI Goal 页：`d` 循环选择当前 bounded catalog 中的死信，`u` 重入队所选精确目标；
- Agent 可在推理链中自主调用同一 Tool；
- BYPASS、PERMISSIVE、MODERATE、STRICT 均由显式权限规则接纳且不产生二次确认；LOCKDOWN 仍拒绝；
- Engine 成功写入后唤醒 terminal outbox worker，不等待下一轮周期 tick。

New UI 的 requeue request 与 run-now 共用单飞控制，避免两个终态队列控制动作并发覆盖用户认知。Bridge 只返回
公开 receipt 字段，剔除 source request、failure 与 dispatch 摘要，并在动作后刷新 Goal 权威快照。

## 协议与兼容性

`pursuit_recovery_actions` 能力新增：

- client：`pursuit/terminal-outbox/dead-letter/requeue`；
- server：`pursuit/terminal-outbox/dead-letter/requeue_result`。

request 只含 `dead_letter_id`。成功 result 必须含与目标一致的公开 receipt；blocked/error 不得伪造 receipt。
JavaScript 严格验证 schema、ID、failure sequence、正有限时间、立即 due 关系和 digest 格式。协议 governance
registry 把两个事件登记为 audited control event，并把变更前 registry digest 加入兼容性账本。

## 验收标准与证据

### Store

- 精确 active target 成功 requeue，旧 failure chain 保持逐字一致；
- Store close/reopen 后 receipt 可认证读取，backlog 不再计为 active dead letter；
- 同一 target/source request 重放返回同一回执且不追加第二条 dispatch；
- 新失败段按 1、2 重新耗尽预算，而不是继承旧全局 sequence；
- 第二个 dead-letter segment 可再次生成独立 receipt 并恢复领取；
- receipt payload 被篡改后，claim 与 catalog 均失败关闭；
- 非当前 target、并发 head 漂移、source request 冲突和倒退时间均不得修改状态。

### 调用与体验

- Agent Tool 注册到 Engine，成功后主动 wake worker；
- CLI 与 Textual TUI 把精确 ID 作为 Tool 参数传递；
- New UI 只能操作当前 catalog 中选中的 ID，协议未协商时不发送并显示 slash fallback；
- Bridge 经 ToolExecution 执行并返回脱敏 receipt，随后刷新 Goal snapshot；
- bypass 不出现高风险二次确认；
- catalog 中处置后的目标消失，再次死信时显示新 `ptfail_...` 与当前预算段次数。

### 聚焦验证命令

只运行本切片相关的小模块，不运行全量测试：

```bash
uv run ruff check <本切片涉及的 Python 文件>
uv run pytest -q \
  tests/unit/test_pursuit_terminal_outbox_dead_letter.py \
  tests/unit/test_goal_panel.py \
  tests/unit/test_main_pursue_dispatch.py \
  tests/unit/test_cli_commands_meta.py \
  tests/unit/test_ui_bridge.py::test_protocol_contract_matches_python_enums \
  tests/unit/test_ui_bridge.py::test_bridge_terminal_dead_letter_requeue_uses_tool_and_public_receipt
node --test \
  frontend/terminal-ui/test/protocol.test.js \
  frontend/terminal-ui/test/state.test.js \
  frontend/terminal-ui/test/goal-pursuit-page.test.js
```

## 自我审视与未完成边界

- requeue 是明确的可逆恢复动作，不是业务接受；后续 HAR-10.8f2g 已用独立 abandon receipt 收口确定不应再投递的记录；
- active catalog 仍受 10000 authority 完整扫描上限与 20 条 UI 投影上限约束；历史处置尚无分页视图；
- terminal outbox 仍是 pull worker，尚未实现 push stream；
- 跨 Harness/Pursuit Store 的最终原子提交、kill-at-every-write-point 矩阵和长时间 soak 仍未完成；
- retention 必须等 disposed history 与引用保护边界明确后再进入 apply，不能用删除替代处置。

后续 HAR-10.8f2g 已交付 exact abandon receipt 与不可领取终态覆盖层。下一最小切片应抽取统一 effective-state
projection 和认证 abandoned history，为 retention preview 提供稳定输入；仍不一次性扩展 retention apply。
