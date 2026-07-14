# Terminal UI 模型工作动态图设计

日期：2026-07-14

## 1. 目标

在新 Terminal UI 中加入清晰、平滑且跨平台的动态图像，让用户无需盯着底栏文字就能确认 Naumi
仍在工作，并能区分模型生成、工具执行、等待权限和取消中的状态。Textual fallback 同步紧凑版
动态核心，保持两套终端界面的语义一致。

本功能只改变本地显示和刷新节奏，不修改后端事件、模型请求、会话持久化或工具执行。

## 2. 方案比较

### 方案 A：Kitty / iTerm2 / Sixel 位图动画

可以显示 PNG/GIF，但协议彼此不兼容；macOS Terminal、Windows 控制台、SSH、tmux 和普通 Linux
终端支持差异巨大。探测失败还可能把控制字节打印进会话。与跨平台目标冲突，不采用。

### 方案 B：单字符 spinner

兼容性最好，但当前 Textual 已经有 braille spinner，用户仍很难把它理解成 Naumi 的工作状态，
也无法直观看出阶段。它只适合作为窄终端降级。

### 方案 C：终端原生字符像素动态图

使用 Unicode 线框、眼睛和旋转核心组成稳定三行图像；每帧仅替换相同宽度的少量字符。窄窗口、
低高度、`TERM=dumb`、非 TTY 或减少动态模式自动降级为单行/静态文本。采用此方案。

该方案不依赖图片协议或第三方库，在 Windows Terminal、PowerShell、Git Bash、macOS Terminal、
iTerm2 和主流 Linux 终端中使用同一渲染链。

## 3. 视觉与语义

宽屏动态图固定三行，示意如下：

```text
   ╭─────╮
   │ ◉ • │   模型工作中 · 生成响应
   ╰──◐──╯
```

四帧循环改变眼睛和核心：`◐ → ◓ → ◑ → ◒`。轮廓为 cyan，眼睛为 yellow，核心在
magenta/blue/cyan/green 中循环；状态文字始终存在，不能只靠颜色表达。

窄屏或低高度只显示一行：

```text
◐ 模型工作中 · 生成响应
```

`TERM=dumb` 使用 ASCII 静态降级：

```text
[o] 模型工作中 · 生成响应
```

状态语义来自已有权威运行态：

| 运行阶段 | 展示 | 是否动画 |
|---|---|---|
| `preparing` / `generating` / `summarizing` | 模型工作中 | 是 |
| `executing` | 工具执行中 | 是 |
| `awaiting_permission` 或存在 permission | 等待权限确认 | 否 |
| `cancelPending` | 正在取消 | 否 |
| 运行结束 | 不展示 | 否 |

这样不会在系统实际等待用户时继续制造“模型正在计算”的假反馈。

## 4. 动画生命周期

新增独立动画控制器，不把定时器散落在渲染函数中：

```text
run/started ──> controller.sync(active=true) ──> 120ms frame tick ──> scheduleRedraw
permission ──> controller.sync(active=false) ─> static waiting frame
resolved ────> controller.sync(active=true)
completed/cancelled/exit ─> stop + clearInterval + frame=0
```

约束：

- 同一时刻最多一个 interval；重复 `run/started` 不创建重复定时器；
- 只有真实 TTY 且未设置 `NAUMI_REDUCE_MOTION=1` 时自动播放；
- 非 TTY、测试快照、CI、`TERM=dumb` 显示稳定静态帧；
- timer 必须 `unref()`，不能阻止 Node 进程退出；
- `exit()`、Bridge 结束和运行终态都清理 timer；
- 动画帧是临时 UI 状态，不写入 SQLite 或 UI snapshot；
- 120ms 一帧（约 8 FPS），通过已有 16ms redraw 合并器限流，不直接写终端。

## 5. 渲染位置与视口稳定

动态图属于 `renderBodyTail()`，紧跟当前时间线而不是写入消息数组：

- 不进入历史回放和复制文本；
- 运行结束自动消失，不残留一条伪消息；
- 宽屏三行和紧凑单行在一次运行/同一终端尺寸内保持固定高度；
- `renderViewportLayout()` 使用相同 tail 行数，滚动锚点计算不会漂移；
- 小于 8 行的 body 或小于 70 列的终端使用单行，避免挤掉对话和输入器；
- 每行继续经过 `wrapAnsiLine()` 和 `visibleWidth()`，不能超出窗口。

## 6. Textual 同步

Textual 已有一个裸 braille `Spinner`。本轮不增加第二个 timer，而是让现有组件复用纯函数
`render_working_indicator_frame()`，显示紧凑核心与“Naumi 工作中”文字：

```text
╭◐╮ Naumi 工作中
```

保留原有 80ms timer、激活/暂停调用点和布局高度。停止时清空，避免影响 Textual footer。

## 7. 文件与接口

Node：

- `src/components/working-indicator.js`：纯状态判定与宽/窄/ASCII 渲染；
- `src/working-animation.js`：可注入 scheduler 的单 timer 控制器；
- `src/state.js`：增加临时 `workingAnimationFrame`；
- `src/render.js`：用动态图替换 `运行中...` body tail；
- `src/index.js`：同步 controller 生命周期并在退出时停止。

Textual：

- `src/naumi_agent/tui/working_indicator.py`：纯 Rich `Text` 帧渲染；
- `src/naumi_agent/tui/app.py`：现有 `Spinner` 复用该函数。

## 8. 测试与真实验证

只运行相关小模块：

- Node 纯渲染：四帧确实变化、去 ANSI 后文字稳定、等待权限静态、宽/窄/ASCII 降级、CJK 宽度；
- Node controller：重复 sync 不重复建 timer、停止清理、减少动态和非 TTY 不启动；
- Node state/render：`run/started` 出现、permission 暂停、completed 消失、滚动 tail 行数一致；
- Textual 纯函数：四帧、中文文字和 Rich 颜色语义；
- 真实 Node smoke：用 fake scheduler 推进一轮，渲染每帧并确认行宽；
- `node --check`、触及 Python 文件 Ruff/py_compile、`git diff --check`。

不运行全量 Node 或 Python 测试。

## 9. 自我审视与边界

- 这是字符像素动态图，不是假装所有终端都能显示 GIF；跨平台可靠性优先。
- 动画不覆盖思考文本开关；`reasoning: off` 仍可显示“模型工作中”，但不会泄露思考内容。
- 工具执行时明确写“工具执行中”，避免模型工作状态失真。
- 权限等待和取消必须静态，避免给用户造成系统仍在自主推进的错觉。
- 暂不增加用户自定义帧、主题或帧率；减少动态只提供环境变量开关，后续可进入 `.naumi` UI 配置。
- 不引入 raster 资源和终端私有控制协议，因此没有图片解码、远程终端或 tmux 兼容债务。
