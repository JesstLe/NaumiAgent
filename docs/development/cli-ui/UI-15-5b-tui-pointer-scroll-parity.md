# UI-15.5b Textual TUI Pointer Scroll Parity

## 1. 交付结论

本切片把 Textual TUI 的全局垂直 pointer sensitivity 从框架默认每事件 2 行收敛为每事件 1 行。
Chat、Activity、History、Browser 等所有 `VerticalScroll` 页面因此获得一致的逐行滚轮定位。

它不复制 UI-15.5a 的 Node 定时器，也不改变键盘方向键、PageUp/PageDown 或组件自身的
`scroll_end()` 行为。

## 2. 权威路径

Textual 的 `ScrollView._scroll_up_for_pointer()` 与 `_scroll_down_for_pointer()` 读取
`App.scroll_sensitivity_y` 计算目标位置。`NaumiApp` 在 `App.__init__()` 完成后显式设置：

```text
TUI_POINTER_SCROLL_LINES = 1.0
```

这是应用级唯一策略，不在每个 `VerticalScroll` 子类重复覆盖 mouse handler。Textual 继续负责边界裁剪、
事件停止与实际 viewport 更新。

## 3. 验收证据

真实 Textual `run_test()` 挂载超出 viewport 的 `ChatPanel` 内容，再派发框架原生
`MouseScrollDown`/`MouseScrollUp`：

- 初始 target 为 0；
- 单次向下事件后 target 精确为 1；
- 单次向上事件后 target 精确回到 0；
- 内容高度被确认大于 viewport，测试不是无滚动空壳；
- Ruff、py_compile 与该模块 pytest 通过。

## 4. 自我审视与剩余边界

- Textual 暴露的是离散滚轮事件，不提供稳定的跨终端 fractional delta；本切片只承诺逐行精度。
- Textual 当前 pointer handler 使用 `animate=False`。强行增加动画会让高频事件叠加多个 animation，
  在没有设备矩阵前可能更难定位，因此本切片不伪装“像素级平滑”。
- UI-15.5a 与 15.5b 已分别限制 New UI 和 TUI 的离散滚动速度，但真实 iTerm2、Terminal.app、
  Windows Terminal 和 Linux 终端录制仍属于 UI-17.4。
