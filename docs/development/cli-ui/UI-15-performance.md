# UI-15 渲染性能、虚拟化与大输出

## 目标

在长会话、高频 token、并发工具、大 diff 和图片 artifact 下保持输入响应、滚动精度和内存有界。

## 子模块

- UI-15.1 Event coalescing（partial）：
  - UI-15.1a 已实现 New UI assistant token/thinking delta 的 8ms/65,536 字符有界合并、语义 identity、
    控制屏障和关键控制事件立即 differential paint；见
    [设计](UI-15-1a-stream-delta-coalescing.md)。
  - 未完成：progress/todo/runtime status 领域合并策略、Textual TUI 高频刷新基线和 UI-15.6 SLO benchmark。
- UI-15.2 Virtual timeline：按 message/card 行高索引和 viewport overscan。
- UI-15.3 Render cache：语义 revision、主题、宽度、fold/focus 全入 key。
- UI-15.4 Artifact paging：代码/diff/log/图片引用分页，不把正文塞入状态。
- UI-15.5 Scroll physics：触摸板限速、亚行累积、无惯性跳跃、follow-tail 状态机。
- UI-15.6 Bench harness：可重复 fixture、CPU/内存/首帧/输入/滚动指标。

## 验收标准

- 10k 消息、1k 工具卡、10MB 日志 fixture 下 RSS 有上限且不线性复制正文。
- token 1000 events/s 时输入 P95 小于 100ms，权限请求不被合并丢失。
- resize 连续 50 次无底栏覆盖、缓存串状态或选择漂移。
- 触摸板慢滑可逐行定位，快速滑动受限且平顺；PageUp/PageDown 仍按页。
- benchmark 基线落盘，超过阈值 CI 失败并输出差异。

## 当前状态

UI-15 保持 partial。现有 redraw scheduler 已限制普通 paint 到约 16ms，UI-15.1a 进一步减少进入 reducer 的
stream delta 数量，但 virtual timeline、artifact paging、完整 cache revision、benchmark 与跨前端性能门仍未完成。
