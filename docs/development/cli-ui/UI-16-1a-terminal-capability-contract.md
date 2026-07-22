# UI-16.1a 终端能力合同与安全协商

## 问题

New UI 过去只判断输入输出是否为 TTY，却无条件发送备用屏幕、Bracketed Paste、隐藏光标和同步输出控制序列。
这会把“交互式终端”和“支持某个 ANSI 扩展”错误地视为同一事实：未知终端、`TERM=dumb`、重定向输出或
不支持 synchronized output 的终端都可能收到不兼容序列。Textual `/doctor` 同时只显示 `TERM`、宽度和
`COLORTERM`，无法解释 New UI 为什么启动、降级或 fallback。

## 权威合同

`frontend/terminal-ui/terminal-capability-contract.json` 是 Python 与 Node 的共同匹配规则 authority：

- profile 字段固定为 interaction、fullscreen、ANSI、色彩级别、Unicode、鼠标协议、备用屏幕、
  Bracketed Paste、光标控制、同步输出、增强键盘、动画、信号模式、home 与终端身份；
- 色彩只允许 `none`、`ansi16`、`ansi256`、`truecolor`；
- 鼠标协议当前只报告 `none` 或 `sgr`，本切片不启用 mouse tracking；
- signal mode 区分 `none`、`posix`、`windows`；
- TERM、TERM_PROGRAM 和 Windows 环境标记的 allowlist 只存在于该合同，不在 Python/Node 两侧复制。

Node 在模块加载时校验合同和最终 profile；Python 使用不可变合同对象并验证同一 wire 字段与派生不变量。
合同作为 wheel runtime asset 发布，source checkout 与安装包走同一语义。

## 协商与失败关闭

- 非 TTY 与 `TERM=dumb` 不具备 ANSI 控制能力；测试专用的
  `NAUMI_TERMINAL_UI_ALLOW_NON_TTY=1` 只能绕过 interaction 判断，不能绕过 ANSI allowlist。
- `NAUMI_ALT_SCREEN`、`NAUMI_BRACKETED_PASTE`、`NAUMI_SYNCHRONIZED_OUTPUT` 可以显式关闭能力；显式开启
  仍必须先通过 ANSI 基线，不能强迫未知终端接收控制序列。
- `NO_COLOR` 关闭色彩；显式 `FORCE_COLOR` 优先，并映射到 ANSI 16、256 或 truecolor。
- CI、`NAUMI_REDUCE_MOTION` 或无法进入安全全屏时禁用 working animation。
- New UI 只有在 `fullScreen=true` 时启动，否则返回非零状态，让 Python launcher 使用 Textual fallback；
  不在失败路径发送备用屏幕、光标或同步输出序列。
- Terminal session 只关闭本次真正开启的序列。Screen painter 仅在 profile 明确支持时使用 mode 2026
  synchronized output，避免曾经的无条件发送。

## New UI 与 TUI 边界

New UI 直接消费 profile 控制 ANSI 生命周期。Textual 自己拥有终端生命周期，因此 TUI 不重复发送 Node
控制序列；它通过共享 Python detector 和 `/doctor` 展示同一能力诊断。这样保持统一事实来源，又不与 Textual
renderer 的内部终端管理冲突。

## 验收证据

- Node capability tests：macOS/Linux baseline、iTerm truecolor/sync、Windows Terminal、TERM=dumb、非 TTY、
  NO_COLOR/FORCE_COLOR、reduced motion、未知终端强制开启失败关闭；
- Node session/painter tests：逐项启停、setup 失败恢复、禁用 profile 零 ANSI 输出、同步输出支持/不支持；
- Python tests：相同 wire profile、Windows 路径与信号、未知终端、色彩与动画、合同损坏/未知 matcher、
  `/doctor` 诊断和窄终端提醒；
- wheel manifest test：合同随 runtime assets 打包；
- 真实本机只读对照：以相同 env 和显式 TTY 输入分别运行 Python/Node detector，wire profile 必须完全一致。

## 未完成边界

本切片没有启用或解析 SGR mouse event，没有实现 Windows Console control handler，也没有替代 UI-16.2 的
Unicode cell-width engine。macOS/Linux/Windows 真实终端发布矩阵仍未执行；因此 UI-16 维持 partial，不能宣称
跨平台发布门已通过。后续分别由 UI-16.2、UI-16.3、UI-16.4 和 UI-17 matrix gate 继续推进。
