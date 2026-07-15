# Terminal UI 闪烁消除设计

日期：2026-07-15

## 1. 问题与证据

新 Terminal UI 已在 `9e619922` 从“每帧清屏”改为按行差分，但真实运行日志仍能证明两类可见闪烁来源：

1. 启动进程 `63733` 在 `2026-07-15T04:27:44.186Z` 以 `153×50` 完成首次全屏绘制；
   仅 19ms 后终端尺寸稳定为 `157×48`，再次执行全屏绘制。两次连续 `CSI 2J` 会形成启动闪屏。
2. 模型工作期间每 120ms 更新一帧，日志中通常有 2 至 3 行变化，流式事件到达时可达到更多行。
   现有 painter 虽把一帧合并为一次 `stdout.write()`，却没有终端同步输出边界；终端可以在解析完半帧后就呈现，
   用户会看到短暂的上下行不同步或画面撕裂。

本轮只修复 Terminal UI 的画面提交，不改变后端事件、消息状态、动画语义、流式速度、TUI fallback 或会话数据。

## 2. 方案比较

### 方案 A：关闭动画或降低帧率

可以降低症状出现频率，但模型流式输出仍会触发多行更新，启动双清屏也仍存在。它牺牲反馈质量却没有修复根因，
不采用。

### 方案 B：保留差分，只压缩每次写入的字符数

减少 I/O 有价值，但一次多行更新仍可能被终端分阶段呈现；启动尺寸变化仍会触发第二次全屏清空。不足以闭环，
不采用。

### 方案 C：稳定首帧 + 同步提交差分帧

采用。启动后的首帧进入短暂稳定窗口，resize 会重新开始该窗口，因此只按最终尺寸绘制一次。每次真实画面写入由
`CSI ?2026h` 与 `CSI ?2026l` 包裹，使支持 synchronized output 的终端在整帧完成后一次呈现；不支持该私有模式的
终端会忽略控制序列，继续使用现有按行差分降级。

## 3. 首帧调度

新增独立 `createRedrawScheduler()`，不把更多 timer 状态塞入 `index.js`：

- 普通帧继续使用 16ms 合并窗口；
- 尚未成功绘制首帧时使用 32ms 稳定窗口；
- 首帧前收到 resize 时清理旧 timer，并从最新尺寸重新计 32ms；
- 普通状态事件只与既有首帧 timer 合并，不反复推迟启动；
- `redraw()` 成功后显式 `markPainted()`，失败不能把首帧误标为完成；
- 退出时 `cancel()` 清理未触发 timer；
- 同一时刻最多存在一个 timer。

32ms 约为两帧显示周期，远低于 Bridge 初始化时间，不会把欢迎页延迟到后端 ready；它能覆盖日志中实测的 19ms
尺寸收敛，又不会引入肉眼可感知的空白等待。

## 4. 原子画面提交

`screen-painter.js` 继续负责完整帧校验、首帧/尺寸变化全绘制、稳定尺寸下按行差分和写失败后的重试语义。
所有实际写入统一经过一个私有提交函数：

```text
CSI ?2026h + frame bytes + CSI ?2026l
```

约束：

- begin、画面和 end 必须位于同一个 `write()` 参数中，不能跨异步边界保持同步模式；
- 没有变化的帧仍然零写入，不能只发送 begin/end；
- `write()` 抛错时不能记住失败帧，下次仍做完整重绘；
- `reset()` 保持原语义；
- 控制序列由 `ansi.js` 集中命名，禁止在 painter 中散落魔法字符串；
- 不使用终端闪烁属性，不改变光标隐藏、alternate screen 或颜色协商。

## 5. 接口与文件

- `frontend/terminal-ui/src/redraw-scheduler.js`
  - `createRedrawScheduler({ onRedraw, setTimer, clearTimer, frameDelayMs, initialSettleMs })`
  - 返回 `schedule()`、`settleInitial()`、`markPainted()`、`cancel()` 与只读状态。
- `frontend/terminal-ui/src/ansi.js`
  - 增加 `synchronizedOutputOn`、`synchronizedOutputOff`。
- `frontend/terminal-ui/src/screen-painter.js`
  - 所有非空画面写入使用同步输出边界。
- `frontend/terminal-ui/src/index.js`
  - 用 scheduler 替换裸 `redrawTimer`；启动和首个 resize 走稳定窗口；退出清理 scheduler。

## 6. 定向测试与真实验收

只运行相关小模块，不运行全量测试：

1. `redraw-scheduler.test.js` 用可控 timer 证明首帧 resize 去抖、普通帧合并、失败不误标、退出清理；
2. `screen-painter.test.js` 证明 full/diff 都在一个同步边界内、none 零写入、失败后完整重试；
3. `terminal-session.test.js` 证明会话进入/退出控制序列没有被破坏；
4. `index-process.test.js` 仅运行无闪烁动画用例，证明真实 Node 子进程在多帧动画中不重复清屏，并产生配对同步帧；
5. `node scripts/check-syntax.js` 检查触及的前端源码；
6. 用真实交互式 PTY 启动 Naumi 新 UI、等待欢迎页、退出，检查日志中稳定尺寸下只有一次初始 full paint，且无
   `render.error` / `terminal_ui.fatal`。

## 7. 验收标准

- 首帧前连续 resize 最终只调用一次 redraw，使用最后一次终端尺寸；
- 启动正常情况下只出现一次 `ANSI.clear`；
- 动画及流式差分不再发送 `ANSI.clear`；
- 每个实际 full/diff write 恰好包含一对同步输出边界；
- 无变化帧不写终端；
- resize 后仍能完整重绘；
- 写失败后下一次仍能恢复完整画面；
- 进程退出不遗留 timer、raw mode、隐藏光标或同步输出状态；
- 上述定向测试、语法检查和真实 PTY 冒烟全部通过。

## 8. 自我审视与边界

- 同步输出协议对支持终端提供真正的原子呈现；未知终端忽略该私有模式时，仍保留当前差分绘制，不会退回逐帧清屏。
- 首帧稳定窗口修复的是启动阶段尺寸抖动，不吞掉运行期 resize；已完成首帧后，resize 仍立即进入常规 16ms 合并。
- 本轮不顺带调整动画图案、颜色、帧率或滚动速度，避免把视觉偏好混入重绘正确性修复。
- 真实 PTY 验收只能证明当前 macOS 终端链路；Windows Terminal、Linux/SSH 的字节级兼容由纯测试覆盖，后续发布矩阵仍需
  在各平台实际终端执行同一冒烟脚本。
