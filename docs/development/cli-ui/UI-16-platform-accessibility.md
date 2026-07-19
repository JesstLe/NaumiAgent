# UI-16 跨终端、无障碍与国际化

## 目标

在 macOS/Linux/Windows 主流终端、TTY/非 TTY、无色彩和中文环境下提供等价可操作体验。

## 子模块

- UI-16.1 Capability probe：color、truecolor、mouse、unicode、alternate screen、signals。
- UI-16.2 Width engine：wcwidth、emoji、CJK、组合字符、ANSI 截断。
- UI-16.3 Platform lifecycle：POSIX signal、Windows console、PowerShell/cmd、路径与换行。
- UI-16.4 No-color/plain：所有状态有文字/符号冗余，不依赖红绿色。
- UI-16.5 Localization：中文优先、文案 key、参数/日志不误翻译、英文 fallback。
- UI-16.6 Accessibility QA：键盘全流程、焦点可见、减少动画、闪烁限制。
  - UI-16.6a 已恢复 working indicator 的结构化运行性能反馈；活动阶段显示受限的“阶段 + 性能”文字，
    权限、交互和取消等待态不携带可能过时的性能数据，详见
    [UI-16.6a working indicator 运行反馈](UI-16-6a-working-indicator-runtime-feedback.md)。

## 验收标准

- mac Terminal、iTerm2、Kitty、GNOME Terminal、Windows Terminal + PowerShell/cmd 矩阵。
- `NO_COLOR`、非 TTY、TERM=dumb 输出可复制的线性文本，不发控制序列。
- 中文/emoji 宽度 golden fixtures 在 Python/Node 两侧一致。
- working animation 可关闭；每个动态图都有文字状态。
- Windows Ctrl+C/关闭窗口能通知 Bridge 并清理子进程。
- 路径含空格、中文、反斜杠、长路径时工具摘要和链接不损坏。

## 当前状态

UI-16 为 partial（16.6a）。终端矩阵、完整 capability probe、平台生命周期、文案 key 与系统化无障碍 QA
仍未交付，不能据此宣称跨平台发布门已经通过。
