# 02 对话时间线与输入器

## 实施状态（2026-07-13）

已完成四个独立切片：“多行输入器与草稿恢复”“时间线跟随与缩放锚点”“用户消息发送生命周期”和“项目输入历史搜索”。

多行输入器与草稿恢复：

- 输入编辑按 grapheme cluster 移动和删除，不会拆散 emoji 或组合字符。
- `Shift+Enter` 插入换行，`Ctrl+Enter` 提交多行内容，普通 `Enter` 保持提交语义。
- bracketed paste 在起止标记跨输入 chunk 时仍作为一次原子编辑，不会因粘贴换行提前发送。
- Composer 根据终端宽度换行，最多显示 6 行，并在小终端中优先保留光标行。
- 草稿按会话写入 UI state v2；v1 快照自动迁移，未知未来版本不会被旧客户端覆盖。
- 正常退出、进程重启和会话切换均保留未提交草稿、光标及垂直首选列。

时间线跟随与缩放锚点：

- 默认 `follow_tail=true`，流式 Agent 输出持续保持最新内容可见。
- `PageUp` 或 `Alt+Up` 上翻后进入 detached 状态；后续输出不改变阅读偏移。
- assistant token 按消息 ID 去重，tool prepare/use/result 按 `tool_call_id` 去重，不会把流式 token 数误当成未读数。
- detached 状态在固定 footer 显示“有 N 条新输出”；`PageDown` 到底、空输入时按 `End`，或按 `Ctrl+L` 可恢复实时跟随。
- 终端宽高变化时按顶部可见消息 ID 恢复阅读位置；缺少 ID 的旧消息按索引回退。
- UI 快照只保留阅读偏移；重启后派生 follow/detached 状态并清空上一进程的未读证据。

用户消息发送生命周期：

- 普通提交立即创建右缩进的本地用户消息并显示“发送中...”，不再等待 Agent 回包后才出现。
- 本地 `request_id` 与 Bridge `user/message`、`run/started`、`error` 关联；确认后原地变为 accepted，不重复追加用户消息。
- 确认前的 Bridge 拒绝和管道异常会把同一消息标为发送失败，并展示可操作的 `/retry`。
- `/retry [request_id]` 使用新请求 ID 原地重试同一气泡，保留尝试次数，不制造重复用户消息。
- UI state 只保存 queued/failed/uncertain outbox，最多 20 条且单条内容受限；accepted 消息仍以后端会话为准。
- 进程重启后 queued 变为 uncertain，并明确提示“可能重复发送”；不会自动重发，只有用户显式 `/retry` 才执行。
- 后端重放的同内容用户消息可以调和一条 uncertain outbox；异常值、超限条目和重复快照应用均被安全处理。

项目输入历史搜索：

- 成功提交的非空输入写入项目目录 `.naumi/terminal-ui-state.json`，同一项目的不同会话和进程重启可以共享。
- UI state 已升级到 v3；v1/v2 会话快照自动迁移，保留旧草稿和折叠状态，未知未来版本仍保持只读保护。
- `Ctrl+R` 打开反向搜索；输入或 bracketed paste 只更新查询，不改变原草稿。
- 匹配按最新优先、大小写不敏感的子串规则执行，并在候选层去重；中文、Unicode 和多行原文可精确恢复。
- 重复 `Ctrl+R`、`Down` 或 `Tab` 选择更早记录，`Up` 选择更新记录；空结果不会关闭面板。
- `Enter` 只把候选放回 Composer，不会发送；用户需要再次按 `Enter` 才提交，避免搜索即执行。
- `Esc` 关闭搜索并恢复原草稿及 grapheme 光标；孤立 ESC 使用短判定窗口，不会被方向键拆包逻辑永久吞掉。
- 项目历史最多保留 100 条、单条最多 200,000 个 UTF-16 代码单元、总计最多 1,000,000 个代码单元；损坏和超限条目在存储边界被过滤。
- 真实 `.venv` Bridge 双进程验收已证明 `/doctor` 可跨重启找回，接受候选不调用后端，第二次 Enter 只调用一次。

本模块仍未整体完成。后续切片依次为：

1. 斜杠命令补全候选的 `Tab/Up/Down/Enter/Esc` 键盘选择。
2. `chat | task` 输入模式、`/task` 创建及对话上下文联动。
3. Bridge v2 幂等请求、断线增量重放和多客户端 outbox 调和。

四个切片的权威实现计划与验证证据见：

- `docs/superpowers/plans/2026-07-13-terminal-multiline-composer.md`
- `docs/superpowers/plans/2026-07-13-terminal-follow-tail.md`
- `docs/superpowers/plans/2026-07-13-terminal-send-lifecycle.md`
- `docs/superpowers/plans/2026-07-13-terminal-history-search.md`

## 1. 目标

建立与成熟编码 Agent 相同的交互体感：用户消息清晰右对齐，Agent 输出稳定流式增长，执行过程可读但不抢占正文，用户滚动查看历史时不会被强制拉回底部，切页后草稿和对话均保留。

## 2. 主时间线模型

时间线只接受标准化条目：

- `user_message`：用户输入，右对齐或右侧缩进，保留发送时间和状态。
- `assistant_message`：Agent 最终内容，左对齐，支持增量更新。
- `activity_group`：思考阶段、计划和工具卡的容器。
- `system_notice`：连接、恢复、警告和错误。
- `completion_receipt`：一次运行的终态摘要。

同一 `run_id` 的条目按 `seq` 排序。流式 token 只能更新对应消息，不得不断创建新行。

## 3. 自动滚动规则

定义 `follow_tail` 布尔状态：

1. 用户提交消息、打开新会话或主动跳到底部时设为 `true`。
2. 用户向上滚动超过一行时设为 `false`。
3. `follow_tail=true` 时，任何增量输出后保持最后一行可见，光标仍位于输入器末尾。
4. `follow_tail=false` 时，不移动视口，底部显示“有新输出”提示及未读事件数。
5. 切换页面不改变该状态；终端缩放后以锚点条目恢复视口，而非使用旧绝对行号。

## 4. 输入器数据模型

```text
ComposerState
  text: string
  cursor_offset: integer (Unicode code point)
  selection: optional range
  mode: chat | task
  history_cursor: optional integer
  draft_id: session + project
  completion: optional command/file/model suggestion
  submitting: boolean
```

输入器必须按 Unicode 字符移动，不允许切断组合字符。渲染宽度使用终端显示宽度，而非字符串长度。

## 5. 编辑与提交规则

- `Enter`：无补全面板时提交；补全面板打开时确认候选。
- `Shift+Enter`：插入换行。
- `Ctrl+Enter`：无条件提交多行内容。
- `Up/Down`：单行且光标在首/末行时浏览历史，否则在文本中上下移动。
- `Ctrl+R`：打开当前项目的输入历史搜索。
- `Esc`：先关闭补全，再清除选择，不直接丢弃草稿。
- 粘贴多行内容使用 bracketed paste，必须作为一次原子编辑处理。

提交后立即生成带 `request_id` 的本地用户消息，状态从 `queued` 变为 `accepted`；Bridge 拒绝时变为 `failed` 并允许重新发送。

## 6. 对话与任务联动

输入模式不是两套会话：

- `chat` 表示用户期望解释、讨论或查询。
- `task` 表示用户授权进入可执行工作流。
- Agent 可在普通对话中提出“转为任务”，但必须由用户确认或符合当前权限策略。
- `/task <内容>` 创建任务后仍在当前时间线继续，任务 ID 写入后续运行事件。
- 任务完成后用户可直接追问，后续消息继承该任务上下文但创建新的 `run_id`。

## 7. 渲染与可访问性

- 用户消息与 Agent 消息的角色差异不能只依赖颜色。
- 代码、diff、命令和路径使用明确的内容类型渲染。
- 长输出按语义块折叠，默认保留标题、状态和首尾摘要。
- 思考过程仅展示阶段性解释和耗时，不展示隐藏推理原文。
- 终端小于 60 列时取消气泡边框，优先保证正文宽度。

## 8. 异常与恢复

- 发送中切页：请求继续运行，返回后恢复增量消息。
- Bridge 断开：保留未确认消息并显示重连状态；不得伪装已发送。
- 进程崩溃：恢复最后持久化草稿，标注是否可能与最后一次提交重复。
- 输出乱序：按 `seq` 缓冲短窗口；缺口超时后显示协议警告并请求重放。
- 超长粘贴：展示字符/行数摘要并要求确认，不静默截断。

## 9. 测试与验收

测试覆盖 Unicode、中文输入、宽字符、组合字符、多行粘贴、历史搜索、发送失败、流式更新、滚动锚点、切页恢复和终端缩放。

验收必须证明：发送后用户消息靠右；Agent 输出自动跟随；用户上滚后不被打断；切到任一命令页再返回，对话、草稿和光标位置不变；普通对话可平滑转任务并继续追问。
