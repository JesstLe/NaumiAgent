# UI-14 QuickOpen、Vim 与完整输入导航

## 目标

提供命令、会话、文件、任务、Agent 和页面统一 QuickOpen；Vim mode 可选且不破坏默认键位。

## 子模块

- UI-14.1 Command index：来源、category、readonly、arg schema、权限风险。
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
