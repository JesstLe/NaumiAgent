# UI-16.2a 共享 Unicode 终端宽度合同

## 1. 用户结果

New UI 与 Textual TUI 现在对中文、全角字符、组合字符、旗帜、keycap 和 ZWJ emoji 使用同一套
终端 cell 语义。换行和截断只发生在完整 grapheme cluster 边界；在 1 列视口无法容纳双宽字符时，
显示单格 `…`，不再输出半个 emoji、孤立变体选择符或超过视口的宽字符。

该切片直接支撑 UI-16 跨终端布局和 UI-17 固定视口发布证据，不改变字体文件、终端字体选择或
操作系统字形 fallback。

## 2. 依赖与权威

- 依赖：UI-16.1a 已提供终端能力协商；UI-17.2f 已提供双端固定视口 capture。
- 共享合同：`frontend/terminal-ui/terminal-width-contract.json`。
- Python 权威实现：`naumi_agent.ui.terminal_width`，使用显式运行依赖 `regex` 的 `\X`
  grapheme segmentation 与 `wcwidth>=0.7.0` 的 grapheme-aware `wcswidth()`。
- Node 权威实现：`frontend/terminal-ui/src/ansi.js`，使用 Node 20 的 `Intl.Segmenter`，
  配合 East Asian Wide、Emoji Presentation、VS16、keycap、regional indicator 与 ZWJ 规则。
- East Asian Ambiguous 固定按 1 格处理；控制字符宽度为 0；emoji presentation 按 2 格处理。

JSON 合同不是可由 UI 改写的主题配置。Python 与 Node 测试都读取同一 corpus；跨运行时测试还会
动态执行生产 Node 模块并逐项对照 Python 结果，避免两套测试各自通过不同答案。

## 3. 实现边界

### 3.1 测量

- ANSI CSI/OSC 控制序列不占可见宽度。
- 组合附加符跟随所属 grapheme，不独立占格。
- `👩‍💻`、家庭 emoji、彩虹旗等 ZWJ 序列作为一个 2 格 grapheme。
- 国旗、keycap、VS16 emoji 作为一个 2 格 grapheme。
- 中文、全角和稳定的 East Asian Wide 字符按 2 格；普通 ASCII 与 Ambiguous 字符按 1 格。

### 3.2 截断与换行

- `truncatePlain()` 与 Python `truncate_to_width()` 会先移除意外控制序列，只处理纯文本；
  带受信任 SGR 样式的 Node 内容必须使用 `truncateAnsi()`。
- `truncateAnsi()` 保留截断前的受信任 SGR 状态并显式 reset，避免样式泄漏到后续行。
- `wrapAnsiLine()` 在换行处关闭并恢复活动 SGR；不会拆分 grapheme。
- 当单个 grapheme 本身比视口更宽时，使用合同规定的 `…`，保证每一物理行仍满足宽度上限。

### 3.3 已接入的生产路径

- New UI 的通用 `visibleWidth`、`charWidth`、`wrapAnsiLine`、`truncateAnsi`、composer 换行、
  working indicator、Agent Control 和 Workbench 截断全部消费新算法。
- Python 共享底栏 `clip_to_width()` 改用新合同，不再逐 code point 截断。
- UI-17.2f 的 Textual compositor capture 和已捕获 New UI frame 都由 Python
  `display_width()` 复核，发布证据不再依赖另一套隐式宽度计算。
- 共享 JSON 合同随 wheel 打包；安装后验证仍可读取相同 corpus。

## 4. 验收证据

- Node 单元覆盖 17 组测量、6 组截断、4 组换行、ANSI 样式闭合与控制序列剥离。
- Python 单元读取同一 JSON，覆盖测量、截断、换行、精确 padding、ANSI/OSC 与数值输入。
- 跨运行时集成测试真实启动 Node，逐项比较 Python/Node 的 width、truncate 和 wrap 输出。
- 相关 composer、working indicator、footer、welcome、组件与 UI-17 terminal capture 聚焦测试通过。
- wheel 隔离构建后包含 `naumi_agent/frontend/terminal-ui/terminal-width-contract.json`，且不依赖
  测试目录才能读取合同。

## 5. 自我审视与明确未完成

- 该合同解决“字符串占多少终端 cell”和“在哪里安全截断”，不控制用户终端实际选择的字体，也不
  保证系统已安装每个 emoji/CJK 字形；字体探测和缺字 UX 属于 UI-16.2b。
- 不同终端若被用户配置为 East Asian Ambiguous=2，仍可能与固定 Ambiguous=1 合同不同；
  UI-16.2b 需要决定是否探测/配置该策略，并加入真实 PTY matrix。
- 当前 Node 宽字符表覆盖主流 Unicode CJK/emoji 范围并由 shared corpus 锁定，但尚未自动从
  Unicode EastAsianWidth 数据生成；Unicode 数据版本更新审计属于 UI-16.2c。
- 本切片没有完成 UI-16.3 平台生命周期、UI-16.4 no-color/plain、UI-16.5 文案本地化或
  UI-17.4 三平台发布矩阵，不能据此宣称 UI-16/UI-17 完成。
