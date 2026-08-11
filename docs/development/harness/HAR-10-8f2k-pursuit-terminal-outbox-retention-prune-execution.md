# HAR-10.8f2k Pursuit 终态 Outbox retention prune 执行

## 目标

HAR-10.8f2j 已持久化非执行 admission、私有恢复快照与逐写点 killpoint。本切片把它收敛为第一个可恢复的物理
prune 闭环，同时保持删除权限不可由 preview 或 admission 隐式获得：

- `retention-prune` 默认只做 byte-stable dry-run；
- 只有精确 `admission_id + admission_sha256`、bypass 权限和显式 `--execute` 同时成立才进入写事务；
- 执行前重新认证 admission、私有恢复快照、当前 protection refs 与全部待删 SQLite 行；
- 完成回执、防复活 member tombstone 和全部 DELETE 在一个 `BEGIN IMMEDIATE` 事务中原子提交；
- 在 receipt、member 及每个 delete 的 before/after 写点注入故障，任何 commit 前中断均机械回滚；
- checkpoint 重放必须识别永久 tombstone，不得重新创建已 prune 的 outbox。

## 用户与 Agent 协议

```text
/pursue outbox retention-prune <ptora_...> <admission-sha256>
/pursue outbox retention-prune <ptora_...> <admission-sha256> --execute
```

Agent Tool 为 `pursuit_terminal_outbox_retention_prune`。无 `--execute` 时返回 strict `dry_run` receipt，不创建 prune
记录、member 或任何 DELETE；`--execute` 在非 bypass 模式内由 Tool 自身拒绝，不触发第二次确认。CLI、兼容 CLI、
Textual TUI 和 New UI 继续复用同一 Tool/Slash 后端。

## 执行前认证

Store 在同一写锁事务中执行以下校验：

1. admission 行原始 payload、索引列、source request 与 recovery JSON 摘要有效；
2. 私有 snapshot schema 版本、字段集合、表名、列名、操作顺序、逐步行数与 candidate plan 完全一致；
3. admission ID/SHA 与调用方提交值精确一致；
4. 每个 outbox 仍是 authenticated abandoned；
5. 当前 protection refs SHA 与 admission 一致；
6. 从当前数据库重建的完整恢复快照与 admission 私有快照逐值一致；
7. 同一 source request 未绑定其他 admission，同一 admission 未产生不同 prune receipt。

任一事实变化均在首个 DELETE 前失败关闭。错误仅返回固定中文类别，不回显 outbox/run/attempt 或私有 payload。

## 原子写入与故障矩阵

事务写入顺序：

1. 插入内容寻址的 completed prune receipt（事务外仍不可见）；
2. 为每个候选插入永久 prune member；
3. 按 admission 的外键安全顺序执行每个非空 DELETE；
4. 核对每条 DELETE 的实际 rowcount；
5. commit。

覆盖的真实故障点包括：

- `before/after_prune_receipt_insert`；
- 每个 candidate 的 `before/after_member_tombstone`；
- admission 中每个 delete step 的 candidate-scoped before/after killpoint。

故障在 commit 前发生时，SQLite 回滚 receipt、members 和所有已执行 DELETE；进程重启后 outbox 仍能通过完整
effective-state/protection graph 认证。故障在 commit 后发生时，completed receipt、members 与删除结果同时存在，重试
返回原 receipt，不会重复执行。

## 防复活 Tombstone

`pursuit_terminal_outbox_retention_prune_members` 保存私有 outbox identity、公开 candidate ID、prune ID 与时间，不引用
已删除 outbox 外键。UPDATE/DELETE 由 SQLite append-only trigger 拒绝。

`save_checkpoint()` 在尝试创建 terminal outbox 前计算确定性 outbox ID；若命中 member，它必须重新认证对应 completed
prune receipt、admission、恢复快照和完整 member 集合，然后返回“不再创建”。缺失 receipt、缺失/替换 member、摘要
变化或 admission 不一致均失败关闭，不能静默复活。

## 公开回执

schema v1 的 prune receipt 只公开：

- prune/admission 内容寻址 ID 与 SHA；
- `dry_run/completed`、是否持久化与是否完成物理 prune；
- candidate、计划/已执行写点、删除行与 tombstone 数量；
- candidate ID、恢复快照 SHA 与行数；
- workspace SHA 与固定决策时间。

公开 JSON/Markdown 不包含 outbox/run/attempt/event identity、source request、绝对路径或原始 payload。

## 验收标准

- 默认 dry-run 前后数据库字节一致，outbox 仍为 authenticated abandoned；
- 非 bypass 即使显式 execute 也在 runner 前被拒绝；bypass 无二次确认直接进入精确写事务；
- 成功后 child/parent outbox 行均删除，admission、completed receipt 与 member 永久保留；
- 同 admission/source 重试返回同一 receipt，并发执行最多提交一次；
- receipt/member/每个 DELETE 的 before/after 故障均完整回滚；
- 重放相同 checkpoint 不会重建 outbox；
- append-only trigger 阻止 member 更新/删除，绕过 trigger 的 member 缺失在读取时被三方对账发现；
- 修改 receipt/index/admission/recovery snapshot 后失败关闭；
- CLI、Textual TUI、Agent Tool、New UI 使用同一参数和 ToolExecution 通道；
- 仅运行 retention/CLI/TUI/New UI 小模块测试，不运行全量测试。

## 自我审视与剩余边界

- 当前为单 SQLite 信任域；若攻击者同时删除 prune receipt、admission、members 和全部相关 authority，仍无外部
  Merkle anchor 证明历史存在；
- admission 私有快照保留被删 payload，满足恢复/审计但不会减少全部数据库体积；后续需设计独立加密 archive 与过期；
- 一次 apply 最多 20 个候选、160 个 delete 写点，尚未提供大批次 cursor/worker；
- 当前恢复依赖 SQLite 原子回滚，不支持跨 Store 原子 prune；
- disposed cursor、push stream、跨 Store terminal commit 和 24 小时 soak 仍未完成。

下一步不继续扩大 retention ARC。应回到跨文档依赖图，在 HAR-10、ARC-04/06、UI-10 与 EVO-06 中选择下一项用户可见
且已具备前置条件的最小纵切；retention 的后续 archive/外部 anchor 作为独立模块再排期。
