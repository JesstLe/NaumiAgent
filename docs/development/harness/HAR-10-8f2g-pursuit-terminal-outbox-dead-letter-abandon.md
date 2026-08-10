# HAR-10.8f2g Pursuit 终态 Outbox 精确死信放弃

## 目标与语义

HAR-10.8f2f 已允许用户把精确死信重新加入恢复队列，但无法收口“该记录确定不应继续投递”的情况。本切片新增
exact abandon：对审查目录中的一个 `ptfail_...` 签发不可变放弃回执，永久撤销其自动领取资格。

abandon 不是 delivered。既有 outbox event chain 仍如实记录“终态事实尚未成功交付”；新的 append-only
abandon authority 是高于 delivery pending 的调度终态。系统不得生成 recovery attempt terminal digest、
不得增加 delivered 计数，也不得把人工决定写成成功投递。

## 向后兼容的终态覆盖层

旧 SQLite 表的 state CHECK 与历史 JSON hash 只认识 pending/delivered。原地扩展枚举会要求重建多个带外键表，
并可能改变历史 canonical JSON。因此本切片不改写旧事件，而新增
`pursuit_terminal_outbox_dead_letter_abandons`：

- receipt ID、dead-letter ID、source request digest 和 outbox ID 均唯一；
- `(outbox_id, failure_sequence)` 外键绑定原 dead-letter failure；
- reason 只能是 `no_longer_required`、`superseded`、`external_resolution`、`invalid_target`；
- receipt payload 与摘要、时间、原因分别冗余校验；
- 插入 receipt 与 failure head 从 active dead-letter 变为 disposed 在同一 `BEGIN IMMEDIATE` 事务完成。

claim、pending recovery directory、backlog 和 active dead-letter catalog 均排除拥有 abandon authority 的记录。
删除 receipt 后，failure head 与 failure chain 不一致并失败关闭；篡改 receipt 会使认证读取和审查目录失败关闭。

## 不可变回执

`PursuitTerminalDeadLetterAbandonReceipt` 绑定：

- 内容寻址 `ptabn_...` receipt ID；
- 精确 `dead_letter_id`；
- source request、prior failure 和最后 idle dispatch 摘要；
- failure sequence、受控 reason、abandoned time；
- 对全部事实的 receipt SHA-256。

相同目标重放返回首次回执；同一 source request 不能绑定另一个目标；已 requeue 的 failure 不能再 abandon，已
abandon 的 failure 也不能 requeue。回执不接受自由文本，避免把密钥、路径或模型输出写入长期审计账本。

## Tool 与用户入口

所有入口复用 `pursuit_terminal_dead_letter_abandon` Agent Tool 和 `AgentEngine.execute_tool()`：

- CLI/Textual TUI：`/pursue outbox abandon <ptfail_...> <reason>`；
- New UI Goal 页：`d` 选择死信，`a` 循环四类原因，`z` 执行；
- Agent 可自主调用同一 Tool；
- BYPASS、PERMISSIVE、MODERATE、STRICT 不产生二次确认，LOCKDOWN 仍拒绝；
- Bridge 与 run-now/requeue 共用单飞队列控制，成功后刷新 Goal snapshot。

公开协议只返回 receipt ID、dead-letter ID、failure sequence、reason、abandoned time 和 receipt digest；内部
request/failure/dispatch 摘要全部剔除。

## 验收标准

- 真实 SQLite 中 permanent dead letter 可 exact abandon，重开 Store 后回执仍可认证；
- 原 outbox 不产生 delivered event，delivered 计数不增加；
- pending recovery directory、backlog、claim 和 active catalog 不再包含该记录；
- 相同请求幂等，不同 disposition 冲突关闭；
- receipt 篡改导致认证和 catalog 失败关闭；
- 四种 reason 之外的值在 Tool、Bridge、Python model 和 JavaScript protocol 均拒绝；
- CLI、Textual TUI、New UI 和 Agent Tool 传递同一精确 ID 与受控原因；
- bypass 不出现高风险二次确认；
- 协议治理注册新 control event，并登记变更前 registry digest。

## 聚焦验证

只运行 terminal dead-letter Store、CLI dispatch、Bridge action、New UI protocol/state/render 小模块，不运行全量
测试。真实 SQLite 测试覆盖首次处置、重放、重开、不可领取和篡改路径。

## 自我审视与后续边界

- abandon authority 当前没有独立历史分页投影；用户能看到动作回执和 active catalog 消失，但尚无 disposed archive；
- SQL 基础 delivery state 保留 pending 是刻意的历史事实，不代表仍可调度；任何新查询若只看 state 而忽略
  disposition overlay 都是缺陷，后续应抽取统一 effective-state query；
- retention apply 仍不能开始，必须先交付 bounded disposed history/preview 与引用保护；
- push stream、跨 Store 原子 terminal commit、kill-at-every-write-point 与 24 小时 soak 仍未完成。

下一最小切片应抽取 terminal outbox effective-state projection，并提供认证的 abandoned history，使 retention
preview 能在不扫描内部表细节的情况下引用处置事实。
