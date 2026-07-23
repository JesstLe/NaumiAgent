# UI-14 QuickOpen、Vim 与完整输入导航

## 目标

提供命令、会话、文件、任务、Agent 和页面统一 QuickOpen；Vim mode 可选且不破坏默认键位。

## 子模块

- UI-14.1 Command index：partial；UI-14.1a 已实现严格 terminal command index，统一来源、category、readonly、
  有界 argument syntax schema 与权限风险，New UI/TUI 均从同一 builder 消费；详见
  `UI-14-1a-authoritative-command-index.md`。
- UI-14.2 QuickOpen：partial；UI-14.2a 已交付 New UI/TUI 命令 provider、fuzzy/中文元数据搜索、
  键盘导航、取消保留草稿，以及仅填入不执行的选择合同；UI-14.2b 已交付本次启动内、只记录规范命令名的
  隐私安全最近使用排序；UI-14.2c 已复用 UI-11 权威 `TaskViewItem` 增加两端任务 provider、关联快照隔离、
  每次打开刷新与只填入 `/tasks detail <id>` 的安全合同；UI-14.2d 已复用 ARC-03.2b2 工作区会话快照
  增加两端会话 provider，选择只填入 `/load <id>`；UI-14.2e 已增加 Engine-owned、后台、可取消、
  workspace 隔离且最多 100k 文件的索引，两端文件 provider 只填入安全 `/read` 模板；UI-14.2f 已复用
  schema v2 Agent Control one-shot snapshot 增加两端 Agent provider，并通过
  `/agents agent <name>` 只读深链定位详情。页面 provider 尚未实现。
  详见 `UI-14-2a-command-quick-open.md`、`UI-14-2b-recent-command-ranking.md` 与
  `UI-14-2c-task-quick-open.md`、`UI-14-2d-session-quick-open.md` 与
  `UI-14-2e-workspace-file-quick-open.md`、`UI-14-2f-agent-quick-open.md`。
- UI-14.3 Input mode：insert/normal/visual，可配置关闭，状态明确可见。
- UI-14.4 Multiline/history：光标、选择、撤销、搜索、IME、Unicode grapheme。
- UI-14.5 Key conflict resolver：平台/终端能力、用户 override、冲突诊断。
- UI-14.6 Discoverability：上下文快捷键条和 `/keybindings` 实际生效视图。

## 验收标准

- 100k 文件索引后台构建、可取消、不会阻塞输入；结果按 workspace 隔离。
- 中文 IME、emoji、组合字符、粘贴、多行和 bracketed paste 不损坏缓冲区。
- Vim off 时现有快捷键完全不变；on 时 Esc/模式切换不触发退出任务。
- QuickOpen 选择写操作先展示权限/参数，不直接执行。
- mac Terminal/iTerm/Kitty/Windows Terminal/PowerShell 的关键键序列有 fixture。

## 当前状态

UI-14.1a 已提供 QuickOpen 的命令事实源并改善现有 slash completion；UI-14.2a-14.2f 已提供两端
命令/任务/会话/文件/Agent QuickOpen、本次启动的隐私安全最近命令排序、实时权威任务/会话快照、
可取消工作区文件索引与 one-shot Agent snapshot，但尚未实现页面 provider、跨启动历史、
typed argument form、Vim mode、完整 composer grapheme 编辑与键冲突诊断，因此
UI-14 保持 partial。
