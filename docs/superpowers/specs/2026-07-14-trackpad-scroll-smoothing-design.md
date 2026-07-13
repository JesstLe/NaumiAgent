# Trackpad Scroll Smoothing Design

## Goal

让新终端 UI 在 macOS 触摸板快速滑动时仍能精确定位上下文：单次手势移动更细，连续滑动速度受控，方向反转立即生效，同时不改变键盘分页导航。

## Root Cause Evidence

新 UI 运行在 terminal alternate screen，但没有启用 mouse tracking。终端会把触摸板滑动转换为 SS3 cursor 序列 `ESC OA` / `ESC OB`。真实 `.naumi/terminal-ui-debug.jsonl` 显示一次约一秒的向下手势会产生几十到上百个事件，部分输入 chunk 还包含两个连续序列。

当前 `index.js` 把每个 SS3 上/下事件交给 `adjustScrollOffset()`，而该函数每次移动半个终端高度。高频事件乘以半屏步长，导致视口瞬间越过目标位置。

## Chosen Interaction

- SS3 上/下滚动每个被接受的事件只移动一行。
- 同方向事件的最短接受间隔为 32ms，最大持续速度约为每秒 31 行。
- 一段手势的首个事件立即响应，不等待计时器。
- 用户反转方向时立即接受首个反向事件，避免“刹不住”或残余惯性。
- 被限流的事件不改变 timeline、不触发重绘，也不写 UI snapshot。
- `PageUp` / `PageDown` 继续移动半屏，用于快速跨页。
- 普通 CSI 方向键继续控制输入历史或多行光标；Inspector、Agent Control 和聚焦任务面板保持各自现有导航。

## Architecture

新增 `scroll-input.js`，提供一个小型有状态过滤器。它只接收方向和单调时间，返回本次是否允许滚动；不依赖全局 `process`、渲染器或 timeline state，因此可以用确定性时钟独立测试。

`index.js` 为主 timeline 创建一个过滤器实例。收到 `INPUT_KEYS.upAlt` / `downAlt` 时：

1. 询问过滤器是否接受该方向。
2. 若拒绝，立即返回。
3. 若接受，调用 `scrollTimeline(state, +1/-1)`。
4. 持久化当前 UI snapshot 并安排一次重绘。

不启用 SGR mouse tracking。该模式会改变终端文本选择行为，且多数终端的滚轮报告仍是离散事件，无法为本次精确滚动提供额外价值。

## Boundaries

- 只处理主时间线的 SS3 滚动输入。
- 不加入人工“惯性”或事件 backlog，避免手势结束后界面仍继续移动。
- 不新增用户配置；32ms 和单行步长作为经过测试的产品默认值。
- 不改变 timeline 的 follow-tail、未读计数、滚动锚点和 session snapshot 数据结构。

## Verification

- 单元测试证明首事件立即通过。
- 单元测试证明 32ms 内的同方向爆发被压缩，达到间隔后可再次滚动。
- 单元测试证明方向反转立即通过。
- 单元测试证明时间异常或非单调时不会解除限流。
- 现有 input tokenizer 与 timeline follow 小模块测试保持通过。
- 进程级定向测试向 stdin 写入真实 SS3 burst，验证一次高频手势只产生细粒度、受控的 scroll offset。
- `npm run check` 验证前端 JavaScript 语法。

## User Experience

快速滑动不再跨越数十个屏幕；缓慢滑动可以逐行定位。需要快速翻页时仍可使用 `PgUp/PgDn`，因此精确滚动与大跨度导航各自保留明确入口。
