# HAR-08.4o3h Sandbox Retry Retention Preview

## 1. 交付结论

本切片在 HAR-08.4o3g 的完整 protection refs 上建立工作区隔离、有界、只读且可校验的 retention
preview。它只选择超过保留期的已对账终态 retry dispatch cohort，不删除、不归档、不更新状态，
也不签发 prune receipt。

共享入口：

```text
/harness eval sandbox retry-retention-preview \
  [--retention-days 1..3650] \
  [--limit 1..20] \
  [--scan-limit 1..100] \
  [--assessed-at <ISO8601>]
```

Agent Tool、New UI 与 Textual TUI 都复用同一 Service/Store 与 Markdown renderer。

## 2. HAR-06 合同继承

本切片继承 HAR-06.5a 的安全语义：

- preview 是 side-effect-free dry-run，永远不构成删除授权；
- 固定 `assessed_at` 和 cutoff，结果稳定、可复查；
- oldest-first，以 `updated_at + retry_action_id` 确定性排序；
- `limit` 与 `scan_limit` 都是硬上限，延后项保持不变；
- 明确区分总计、打开、终态、年龄合格、已扫描、已选择与延后；
- 扫描截断时不声称未扫描记录已经通过权威校验；
- 后续执行必须重新校验持久状态，而不能消费过期 preview。

与 Session retention 不同，retry cohort 没有载荷字节可安全估算。本切片不虚构“可释放空间”，只报告
事实行引用与 H5a 数量。

## 3. 数据选择

Store 使用显式 `BEGIN DEFERRED` 只读事务：

1. 汇总当前 workspace 的 dispatch 总数、open 数、terminal 数和年龄合格数；
2. 仅查询 `completed|failed|cancelled` 且 `updated_at <= cutoff_at` 的 dispatch；
3. 按最旧优先扫描至 `scan_limit`，只选择前 `limit` 项；
4. 对每个选择项复用 4o3g 权威链校验；
5. `rollback` 结束读事务，不调用 schema 初始化或任何状态转换。

`pending|claimed`（包括 live、expired recovery、reconcile_required）永远不进入候选，只计入
`open_dispatch_count`。

## 4. Protection refs 修正

4o3h 审查发现 4o3g 只公开了 `source_ticket_id`，没有携带源 ticket digest。现在详情事务还会回读、
校验并保护 cancel chain 的 source ticket：

- source ticket 必须存在且为 `cancelled`；
- ticket authority 必须与 accepted retry receipt 的 source authority 一致；
- ticket request digest 进入公开 `source_ticket` protection ref。

因此每个无 H5a 的终态候选至少有六项保护引用：

1. dispatch；
2. retry receipt；
3. cancel receipt；
4. Request Manifest；
5. source ticket；
6. current retry ticket。

每个 H5a result 再增加一项 `h5a_sample` 引用。

## 5. Tamper-evident preview

每个 candidate 包含：

- `hsrrp_` candidate ID 与 canonical SHA-256；
- retry action/dispatch/terminal state；
- 更新时间与由 `assessed_at` 机械计算的 age；
- receipt/request/batch/suite identity；
- detail snapshot ID/SHA；
- 完整 protection refs 与独立引用摘要。

整个 preview 使用 `hsrrpv_` ID 与 canonical SHA-256，并绑定 workspace hash、policy、cutoff、所有统计
和候选。严格模型重新校验：

- candidate 必须早于 cutoff；
- age 必须与 assessed/updated 时间完全一致；
- 排序、计数、deferred、scan/selection truncation 一致；
- protection refs、candidate、preview digest 一致；
- unavailable 投影必须为空且所有统计归零。

## 6. 用户体验

Renderer 明确使用“只读候选”：

- 不展示 owner、actor、reason、execution authority 或 workspace 路径；
- 不显示“确认删除”按钮；
- 不提供 prune 命令；
- 显示每类保护引用数量与 digest；
- 明确 open dispatch 仍受保护；
- 明确 preview 本身永远不能执行删除。

## 7. 验收标准

- 真实 Git + SQLite 构造三项旧终态和一项 open dispatch；
- `limit=2, scan_limit=2` 时 oldest-first、eligible/scanned/selected/deferred 均准确；
- 预览前后逐表 SQLite facts 完全一致；
- open 和未过期 terminal 不进入候选；
- 相同 ID 的两个 workspace 不串读；
- 空数据库不被初始化；
- source/current ticket 与每项 H5a 都进入 protection refs；
- 篡改引用 digest 被严格模型拒绝；
- Tool 为 `read_only + concurrency_safe`；
- Slash 与 New UI/TUI 共享 submit 通道；
- 聚焦 ruff、compileall、Python/JavaScript 小模块测试通过。

## 8. 后续边界

下一切片 HAR-08.4o3i 才能设计 prune receipt authority。它至少必须绑定：

- preview ID/SHA 与 candidate ID/SHA；
- 当前 workspace；
- fresh `assessed_at`；
- dispatch 当前 state/epoch/updated_at/request digest；
- 所有 protection refs；
- 明确的用户或治理 actor 与审计 reason。

签发 receipt 前必须重新读取并验证 cohort 没有新增引用。即使 receipt 存在，真正删除仍应作为后续
独立切片实现，并在一个写事务中按外键/共享引用顺序执行；4o3h 不包含这些能力。
