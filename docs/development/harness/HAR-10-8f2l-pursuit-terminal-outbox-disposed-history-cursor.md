# HAR-10.8f2l Pursuit 终态 Outbox 已处置历史游标

## 目标与依赖

HAR-10.8f2h 已建立 `abandoned` effective-state 与最新 20 项的认证历史，但 `truncated` 只能说明“还有内容”，
用户无法继续审查。本切片在不扩大 retention、archive 或 ARC 范围的前提下，交付可验证的历史分页：

- Store 从完整认证 disposed authority 生成稳定页；
- cursor 绑定 Store、查询策略、完整历史快照和页位置；
- Goal Tool、CLI/Textual TUI 与 New UI 复用同一查询边界；
- 协议通过独立 capability 发布 schema v5，同时为旧客户端降级到 schema v4。

直接依赖 HAR-10.8f2h。HAR-10.8f2i-k 的 retention authority 不依赖本 cursor，也不能把 cursor 当作删除准入。

## Store authority 与 cursor

`PursuitStore.terminal_outbox_disposed_page(limit, scan_limit, cursor)` 先执行与原 catalog 相同的完整认证：

1. 有界扫描 failure head、failure event 与 abandon receipt 的候选并集；
2. 逐项验证 outbox、dispatch、failure chain 与 abandon receipt；
3. 只保留 effective-state 为 `abandoned` 的记录；
4. 按 `(abandoned_at, receipt_id)` 逆序稳定排序；
5. 以全部 receipt digest 和总数计算 snapshot SHA，再按 offset 切页。

cursor 是 canonical JSON 的 base64url envelope，包含版本、offset、`limit`、`scan_limit`、Store 路径摘要与 snapshot
SHA，并带内容摘要。读取时严格复验字段集合、类型、范围、查询参数、Store 与当前 snapshot；历史新增、删除、篡改、
Store 变化、查询策略变化、越界或损坏都会失败关闭。cursor 不是秘密、授权令牌或对恶意客户端的 MAC；真正可见的
记录仍必须经过 Store authority 认证，cursor 只提供内容完整性、查询绑定与陈旧检测。

边界固定为 `limit=1..100`、`scan_limit<=10000`、cursor 最长 1024 字符；布尔值不能冒充整数。SQLite 查询使用
参数绑定，公开 cursor 不包含绝对路径、outbox/run/attempt identity 或原始 payload。

## 四端消费与兼容性

- Agent Tool：`goal_list(terminal_outbox_disposed_cursor=...)`；
- CLI 与 Textual TUI：`/goal outbox history [cursor]`；无 cursor 返回第一页；
- New UI：Goal 页用 `}` 打开下一页、`{` 返回上一页，页栈只存在于当前进程；
- Bridge：`goal_panel.terminal_outbox_disposed_cursor` 进入共享 builder；
- schema v5：增加 current/next cursor、`disposed_has_more` 与隔离的 `disposed_warning`；
- capability：`goal_terminal_outbox_disposed_cursor`。支持者接收 schema v5；只有 `goal_snapshot` 的旧客户端接收
  移除新字段后的 schema v4；未协商能力却提交 cursor 时直接拒绝，不查询 Store。

历史 cursor 失败不会把健康 worker 伪装为不可用：disposed 区域单独显示“返回第一页”，backlog、dead-letter 和
worker health 继续呈现真实状态。Store 已不存在时携带旧 cursor 同样返回失效警告，不创建新数据库。

## 验收标准

- 真实 SQLite 两页结果无重复、无遗漏，排序和 total 稳定；
- 损坏、查询参数不匹配、snapshot 变化、Store 缺失与越界 cursor 均失败关闭；
- `has_more`、`next_cursor` 与 `truncated` 在 Python/Node 双边保持一致；
- disposed 读取失败不污染 worker/backlog 健康事实；
- Agent Tool、CLI/TUI 与 New UI 进入同一 Store 查询；
- New UI 的历史页栈不与 interaction 页码混用，重开进程回到第一页；
- 旧客户端仍收到可解析 schema v4，新客户端通过协商才可发送 cursor；
- 仅运行 Store、Goal、Bridge、协议、状态与页面的小模块测试，不运行全量测试。

## 自我审视与未完成边界

- cursor envelope 使用公开摘要，不承担身份认证；任何写入或敏感读取仍必须使用独立权限与 authority；
- 每次翻页会重新扫描并认证最多 10000 个候选，保证快照正确但不是超大规模 archive 查询；
- 当前只有当前进程内的上一页栈，不持久化导航状态，符合“新启动为默认状态”；
- 单 SQLite 信任域之外仍缺少独立加密 archive、外部 Merkle anchor；
- push stream、跨 Store 原子 terminal commit、完整 Supervisor 与 24 小时 soak 仍未完成。

下一步应回到跨文档依赖图选择最小用户可见纵切，不继续扩张 retention ARC。
