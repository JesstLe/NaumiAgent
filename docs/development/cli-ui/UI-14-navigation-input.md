# UI-14 QuickOpen、Vim 与完整输入导航

## 目标

提供命令、会话、文件、任务、Agent 和页面统一 QuickOpen；Vim mode 可选且不破坏默认键位。

## 子模块

- UI-14.1 Command index：partial；UI-14.1a 已实现严格 terminal command index，统一来源、category、readonly、
  有界 argument syntax schema 与权限风险，New UI/TUI 均从同一 builder 消费；详见
  `UI-14-1a-authoritative-command-index.md`。
- UI-14.2 QuickOpen：fuzzy、最近使用、workspace 文件、会话/任务/Agent provider。
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

UI-14.1a 已提供 QuickOpen 的命令事实源并改善现有 slash completion，但尚未实现全屏 QuickOpen、跨 provider 搜索、
最近使用排序、typed argument form、Vim mode、完整 grapheme 编辑与键冲突诊断，因此 UI-14 保持 partial。
