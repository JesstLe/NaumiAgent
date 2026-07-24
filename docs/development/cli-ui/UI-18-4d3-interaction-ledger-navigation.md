# UI-18.4d3 Interaction 账本页内导航

## 目标

把 Goal 页面中“最近 50 条只读文本”升级为共享 authority 驱动的交互账本：New UI 可以按状态筛选、
稳定翻页、选择记录并在页内读取完整公共详情；Textual TUI/CLI fallback 可以通过同一
`goal_list` Tool 和不透明 cursor 继续读取后续页。任何界面都不得直接查询 SQLite、保存 owner ID，
或从展示文本反推可取消/可接管状态。

## Authority 与 cursor

`HarnessStore.list_interactions_page()` 是唯一分页读取原语：

1. 按 canonical workspace、`subject_kind=pursuit` 和当前 Goal 已关联的 Pursuit ID 集合隔离；
2. 使用 `rowid DESC` keyset pagination；新写入记录不会挤入已打开 cursor 的后续页；
3. cursor 绑定 workspace 摘要、subject kind、完整 subject ID 集合、状态筛选和最后 rowid；
4. cursor 使用版本化、摘要校验的 URL-safe opaque envelope；格式、工作区或筛选不一致时失败关闭；
5. 单页最多 50 项，Goal 页面默认 10 项，不进行隐式 expire、takeover 或状态修复。

## 共享公开投影

`GoalPursuitSnapshot` schema v2 增加：

- `interaction_filter`、当前/下一 cursor 和 `interaction_has_more`；
- 有界 `selected_interaction`，包含公共选项、自定义输入能力、终态答案、sequence、owner epoch、
  问题期限与租约是否过期；
- 不包含 owner ID、answered_by、原始持久 payload、reasoning 或 secret。

旧 schema v1 仍由 New UI consumer 兼容读取，并按 `all`、无 cursor、无选中详情降级。

## New UI 交互

- `j/k` 选择账本记录，`Enter` 按稳定 interaction ID 向 Bridge 请求页内详情；
- `f` 循环 `all/pending/answered/expired/cancelled`，切换筛选时清空 cursor 栈；
- `n/p` 使用后端 next cursor 和本地上一页栈翻页；
- `r` 重读当前页，`Esc` 先关闭详情，再返回会话；
- pending、answered、expired、cancelled 使用不同语义颜色；动作提示仍来自 authority 的
  `can_cancel` / `can_takeover`，不在前端猜测。

## TUI fallback

`/goal interaction list [all|pending|answered|expired|cancelled] [cursor]` 经共享 slash dispatcher
调用 `goal_list` Tool。输出包含当前状态筛选、同一批公共记录和下一页命令。详情、取消、接管继续复用
既有 `/goal interaction detail|cancel|takeover <id>`，不增加第二条写路径。

## 验收标准

- 真实 SQLite 中 11 条以上记录可稳定读取两页；第一页打开后新增记录不造成第二页重复或遗漏；
- cursor 改用于不同筛选、subject 集合或 workspace 时失败关闭；
- selected detail 只允许当前 Goal 已关联 Pursuit，且协议归一化后不保留 private/owner 字段；
- New UI 可键盘选择、展开详情、筛选、前后翻页并关闭详情；
- TUI fallback 能复制下一页命令读取相同 authority 结果；
- Python ruff、定向 Harness/Goal/Bridge/协议测试与 Node Goal/状态/协议测试通过，不运行全量测试。

## 保留边界

- 本切片不实现交互优先级、全文搜索、跨 Goal 全局浏览或主动通知；
- TUI fallback 是命令式下一页，不保存跨命令的本地上一页栈；
- 页内详情仍只读；取消和 takeover 保持现有 Tool/Bridge 写路径；
- interaction 与 Pursuit Store 的跨库原子性仍属于 ARC-05/ARC-08。
