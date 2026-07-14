# UI-15 渲染性能、虚拟化与大输出

## 目标

在长会话、高频 token、并发工具、大 diff 和图片 artifact 下保持输入响应、滚动精度和内存有界。

## 子模块

- UI-15.1 Event coalescing：token/thinking/progress 按帧合并，控制事件不延迟。
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
