# HAR-10.8f2i Pursuit 终态 Outbox 保留策略只读预演

## 目标

HAR-10.8f2h 已提供唯一的 `pending/delivered/abandoned` effective-state 与认证 disposed catalog，
但“记录年龄足够旧”仍不能被解释为“可以删除”。本切片建立 retention apply 之前的只读安全边界：

- 只选择在固定评估时间下，已超过保留期的 authenticated `abandoned` outbox；
- 为每个候选构建脱敏、可认证的保护引用图；
- 签发内容寻址的 preview/candidate 回执；
- 明确区分“年龄与处置状态合格”和“已获删除授权”；
- 通过 Agent Tool、CLI、Textual TUI 与 New UI 共用 Slash 通道展示同一结果。

本切片不会删除、归档、压缩或改写 SQLite 中的任何记录，也不会签发 prune/apply authority。

## 固定评估与有界选择

`PursuitStore.preview_terminal_outbox_retention()` 接受显式策略：

| 字段 | 范围 | 语义 |
|---|---:|---|
| `assessed_at` | 有限正时间戳 | 本次审计的唯一时间基准 |
| `retention_days` | `1..3650` | `cutoff = assessed_at - days` |
| `limit` | `1..20` | 最多签发的候选数 |
| `scan_limit` | `limit..100` | 最多认证的年龄合格记录数 |

候选按 `(abandoned_at, receipt_id)` 正序稳定选择，即最旧记录优先。Preview 同时返回
total/pending/delivered/abandoned、eligible/scanned/selected/deferred 与两个截断事实；它不会用有限扫描结果
冒充全量认证结果。

不存在数据库或数据库为零字节时，读取返回空 preview 且不初始化 schema。已有数据库通过
`BEGIN DEFERRED` 与 connection-local `query_only` 打开；正常、异常和篡改失败路径都 rollback 并关闭连接。

## 保护引用图

每个候选必须重新认证 effective-state、outbox/dispatch/failure/requeue/abandon authority、recovery attempt、
boundary decision 与 checkpoint pointer。Preview 仅公开以下引用类别：

- recovery attempt snapshot/event；
- outbox snapshot/event；
- dispatch snapshot/event；
- failure head/event；
- requeue/abandon receipt；
- boundary decision；
- checkpoint pointer。

内部 outbox/run/attempt/event identity 不离开 Store。公开的 `reference_sha256` 是
`sha256(kind + private identity)`，`fact_sha256` 绑定已认证事实内容；引用按 kind/hash 稳定排序、去重并限制为
每候选最多 5000 项。删除或篡改任一可发现 authority 会让预演失败关闭。

历史 checkpoint payload 目前没有独立 append-only event archive，因此 `checkpoint_pointer` 只绑定 outbox 中已认证的
checkpoint 指针；它不能被宣传为 checkpoint 历史内容证明。

## 防篡改 Preview

公开模型使用 strict/frozen Pydantic schema v1：

- `ptorp_...` candidate ID 来自 candidate canonical JSON SHA-256；
- `ptorpv_...` preview ID 来自完整 preview canonical JSON SHA-256；
- workspace 只公开路径 SHA-256，不公开绝对路径；
- candidate 明确写入 `eligibility_reason=abandoned_before_cutoff`；
- candidate 同时写入 `protection_reason=protected_pending_apply_authority`。

因此“进入 preview”只表示年龄和已处置状态满足候选规则。所有候选仍受保护，未来 apply 必须重新读取并认证
完整引用图、验证 preview policy/identity，并签发独立的变更 authority；本 preview 永远不能直接执行删除。

## 双通道与用户入口

Agent 可自主调用只读 Tool：

```text
pursuit_terminal_outbox_retention_preview
```

用户可在 CLI、Textual TUI 和 New UI 使用同一命令：

```text
/pursue outbox retention-preview
/pursue outbox retention-preview --retention-days 45 --limit 5 --scan-limit 10
/pursue outbox retention-preview --assessed-at 2026-08-11T08:00:00+08:00
```

CLI 与 TUI 共用严格 option parser：未知参数、重复参数、缺值、非十进制整数和不一致 limit 均在 ToolExecution
之前拒绝。Tool 标记 `read_only + concurrency_safe`，在所有权限模式下无需二次确认。New UI 不复制 retention
逻辑，只把完整命令发送到后端共用 Slash 通道，并渲染同一 Markdown 回执。

## 验收标准

- 真实 SQLite abandoned 记录超过 cutoff 后按最旧优先进入 preview；较新记录不进入；
- pending/delivered/abandoned 汇总严格互斥且等于 total；重叠 authority 失败关闭；
- 每个候选认证完整保护引用图，私有 identity/workspace path 不出现在公开 payload；
- preview/candidate/ref 任一事实被修改后 strict schema 拒绝；
- 缺失或篡改 abandon authority 时不返回候选；
- 空数据库预演不创建或修改文件；已有数据库执行前后字节一致；
- Agent Tool 与 Engine 使用同一 Store/backend，且 Tool 在所有权限模式中保持只读；
- CLI、旧 CLI 兼容入口、Textual TUI 使用同一参数语法；
- New UI 使用共用 Slash channel，不在前端伪造 prune/apply；
- 只运行 Store/Tool/CLI/TUI/New UI 的相关小模块测试，不运行全量测试。

## 本轮验证

- `tests/unit/test_pursuit_terminal_outbox_retention.py`：真实 SQLite、篡改、空文件、严格策略、Engine 与 Agent Tool；
- `tests/unit/test_main_pursue_dispatch.py`：CLI ToolExecution 参数路由；
- `tests/unit/test_cli_commands_meta.py`：兼容 CLI 共用 ToolExecution 路由；
- `tests/unit/test_tui_pursuit_retention.py`：Textual TUI 共用参数与 Tool 路由；
- `frontend/terminal-ui/test/state.test.js`：New UI 保持共用 Slash channel；
- 一次临时真实 SQLite 场景贯通 `Store → Engine → Agent Tool → 中文回执`，并比较数据库前后字节。

## 自我审视与未完成边界

- 本切片只有 preview，没有物理 prune、apply receipt、恢复清单或故障中断恢复；任何删除仍是不允许的；
- protection refs 是有界单 Store 图，不是外部 Merkle anchor；攻击者若同时移除全部可发现 authority，当前信任域外
  没有独立存在性证明；
- checkpoint pointer 尚未绑定历史 checkpoint payload archive；未来 apply 前必须先补该证据边界或明确排除；
- disposed history 与 retention preview 尚无 cursor 翻页；本轮最多扫描 100、选择 20；
- push stream、跨 Store 原子 terminal commit、kill-at-every-write-point 与长时 soak 仍未完成。

下一最小切片不能直接做删除。应先设计 retention apply admission：精确绑定 preview SHA、重新认证全部引用、生成
可恢复变更计划和拒绝原因，并加入 kill-before/after-each-write 故障矩阵；只有 admission 与恢复协议通过后，才允许
独立实现物理 prune。
