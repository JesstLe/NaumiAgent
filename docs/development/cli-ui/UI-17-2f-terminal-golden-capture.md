# UI-17.2f 双端终端 Golden Capture

## 1. 目标

建立 Naumi 自有的固定视口捕获原型：同一个 UI-17 typed fixture 必须分别经过 New UI 的生产
`reduceServerEvent + renderScreen` 和 Textual TUI 的生产 `EngineEventAdapter + TUIRenderer + compositor`，输出
ANSI 与纯文本证据。该能力用于发现语义丢失、颜色消失、终端宽度越界和非确定渲染，不以截图相似替代后端合同。

本切片落实 CC-02 对 `theswerd/brainless` 的参考结论：借鉴真实终端 capture 方法，不引入 React DOM、Next、
shadcn 或上游 capture 脚本。

## 2. 权威输入与真实渲染路径

`tests/fixtures/ui17/terminal-run-lifecycle-golden.json` 仍是唯一输入，新增的 `capture` schema v1 只声明：

- 固定视口 `100 × 40`；
- 双端都必须可见的有限语义锚点；
- 不复制 tool、receipt 或提交内容。

New UI 由 `frontend/terminal-ui/src/golden-capture.js` 调用实际 state reducer 和 ANSI renderer。Textual 端由
`src/naumi_agent/ui/terminal_capture.py` 使用生产组件、生产 CSS、TUI renderer 和 Textual compositor；capture host
只替代 `AgentEngine` 启动，不重新实现组件或视觉语义。

## 3. 输出合同

开发者可运行：

```bash
python -m naumi_agent.ui.terminal_capture \
  --fixture tests/fixtures/ui17/terminal-run-lifecycle-golden.json \
  --output .artifacts/terminal-golden
```

成功后目录包含：

- `new-ui.ansi`、`new-ui.txt`；
- `tui.ansi`、`tui.txt`；
- `manifest.json`，schema 为 `naumi.terminal-golden-capture.v1`。

manifest 记录 fixture SHA-256、视口、surface/renderer 身份、ANSI/text SHA-256、行数、最大 cell width、锚点与
缺失项。它不记录当前时间、输出目录或机器绝对路径，因此同一依赖版本和 fixture 下可以逐字复验。文件先完成
内容与 digest 校验，再以原子替换发布；manifest 最后写入，避免把未完成证据误认为成功 capture。

## 4. 确定性与失败边界

- New UI 状态栏时钟固定为 capture 输入 `12:34:56`；真实运行仍使用本地时间。
- 运行卡耗时取 authoritative completion receipt，而不是两次 reducer 调用之间的墙钟差。
- Textual 使用固定 truecolor console、固定 cell 尺寸和完整 compositor frame。
- 两端必须恰好输出 `height` 行，任一行不得超过 `width` cells。
- fixture 大于 2 MB、schema 缺失、尺寸越界、Node 不可用、Node 超时、frame digest 不一致或任一语义锚点缺失时
  失败，不发布 manifest。
- ANSI 与纯文本分别摘要；纯文本不得包含 CSI 色彩序列。
- Textual 私有 compositor API 变化时明确失败，不静默退化成 FakePanel 或第二套 renderer。

## 5. 验收证据

- Node 聚焦测试覆盖逐字确定性、ANSI/text、视口边界、语义漂移和坏 fixture。
- Python 聚焦测试连续捕获两次，比较 manifest 与四个 frame 文件逐字相同。
- Python 还覆盖缺失锚点、非法尺寸以及失败前不创建输出目录。
- 真实命令生成双端 100×40 frame；两端均包含“执行定向验证”“bash_run”“定向验证通过”，且 manifest
  `semantic_parity.passed=true`。
- 本切片只运行相关模块测试、Ruff、Python compile 与 Node syntax check，不运行全量测试。

## 6. 自我审视与未覆盖范围

这不是 UI-17 发布门，也不是像素级视觉基线。当前原型证明 conversation/tool/receipt 的生产渲染路径可稳定
捕获，但尚未覆盖：

- permission、interaction、subagent、Harness detail、diff 与数学公式的专用 frame；
- 键盘/IME/触摸板输入时序和动画帧；
- macOS Terminal/iTerm2、Linux terminals、Windows Terminal/ConPTY 的真实 PTY capture；
- 不同色彩能力、字体和 Unicode 版本造成的终端差异；
- TUI 与 New UI 的布局可以不同，当前只要求共享语义锚点，不要求 ANSI digest 相同。

下一步应把此 capture 作为 CC-02 current/Ink 同 fixture 对照和 UI-16 跨平台矩阵的测量入口；不得先复制更多
Brainless 组件，也不得用当前单个 frame 宣称 UI-17.2 完成。

UI-16.2a 已在此基础上补充 Python/Node 共享 Unicode cell 合同，并把 frame 宽度复核切换到
`naumi_agent.ui.terminal_width.display_width()`；CJK、组合字符、旗帜、keycap 与 ZWJ emoji 不再
依赖两端各自的隐式逐字符估算。真实 PTY/字体/Ambiguous 宽度矩阵仍属于 UI-16.2b 与 UI-17.4。
