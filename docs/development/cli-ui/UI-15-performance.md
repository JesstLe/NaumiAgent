# UI-15 渲染性能、虚拟化与大输出

## 目标

在长会话、高频 token、并发工具、大 diff 和图片 artifact 下保持输入响应、滚动精度和内存有界。

## 子模块

- UI-15.1 Event coalescing（partial）：
  - UI-15.1a 已实现 New UI assistant token/thinking delta 的 8ms/65,536 字符有界合并、语义 identity、
    控制屏障和关键控制事件立即 differential paint；见
    [设计](UI-15-1a-stream-delta-coalescing.md)。
  - 未完成：progress/todo/runtime status 领域合并策略、Textual TUI 高频刷新基线和 UI-15.6 SLO benchmark。
- UI-15.2 Virtual timeline（partial）：
  - UI-15.2a 已为默认 New UI 增加共享 message/card 行高前缀索引、二分 viewport 定位、单卡 overscan、
    generation/resize 失效和 legacy 等价诊断；10k+1k release fixture 的 warm deep-scroll P95 从历史
    92.746ms 降至 0.235ms。见[设计与证据](UI-15-2a-virtual-timeline-row-index.md)。
  - UI-15.3a 已让 append 与单消息流式变化增量更新现有索引，不再因 token delta 重绘全部历史；见
    [语义 revision 与增量更新](UI-15-3a-semantic-render-revisions.md)。
  - 未完成：任意位置 splice/Fenwick tree、Textual TUI 对照和冷建索引后台化。
- UI-15.3 Render cache（partial）：
  - UI-15.3a 已实现单调全局/单消息 revision、1,024 条有界 mutation journal、溢出 fail-closed 屏障，
    并接入 assistant/thinking/tool 高频变化路径；10k+1k 深滚动 100 次 token render P95 为 0.282ms。
  - 未完成：theme/capability token、完整 focus/selection revision 与跨 renderer contract。
- UI-15.4 Artifact paging（partial）：代码/diff/log/图片引用分页，不把正文塞入状态。
  - UI-15.4a 已实现超长文本工具输出的执行时归档、会话隔离、逐页摘要校验、共享
    `/tool-output` 命令和 New UI/TUI 入口；见
    [设计与证据](UI-15-4a-tool-output-paging.md)。
  - 未完成：全屏 artifact viewer、快捷翻页、二进制/图片/diff contract、容量配额与模型历史卸载。
- UI-15.5 Scroll physics：触摸板限速、亚行累积、无惯性跳跃、follow-tail 状态机。
- UI-15.6 Bench harness：可重复 fixture、CPU/内存/首帧/输入/滚动指标。
  - UI-15.6a 已实现 current renderer 的 `smoke|release` 可重复 fixture、三场景 JSON 指标与
    fixture digest，作为 UI 优化和 CC-02 Ink 实验的共同对照；见
    [设计与运行方式](UI-15-6a-current-renderer-benchmark.md)。
  - UI-15.4a 将 release fixture 升级为生产分页协议 v2；原始 10MB 直传 RED 证据继续保留，
    新增分页 GREEN 证据，避免用修改后的 fixture 覆盖历史基线。
  - CC-02.1a 已让实验 Ink renderer 复用相同 v2 fixture、digest 和三场景 JSON 合同；首轮结果显示
    deep-scroll 的 viewport slicing 方向有价值，但 tail/paged P95 明显退化，因此没有替换默认 renderer。
  - CC-02.2a 已修正超大 offset 空视口、加入五类核心视图语义并重新测量；Ink deep-scroll P95 仍更快，
    但 tail/paged 分别约慢 25.6/52.8 倍，且全量 presentation 投影仍为 O(n)。
  - 未完成：输入/token/resize、Textual TUI、达到 production 语义 parity 后的 Ink 对照和跨平台 CI 阈值。

## 验收标准

- 10k 消息、1k 工具卡、10MB 日志 fixture 下 RSS 有上限且不线性复制正文。
- token 1000 events/s 时输入 P95 小于 100ms，权限请求不被合并丢失。
- resize 连续 50 次无底栏覆盖、缓存串状态或选择漂移。
- 触摸板慢滑可逐行定位，快速滑动受限且平顺；PageUp/PageDown 仍按页。
- benchmark 基线落盘，超过阈值 CI 失败并输出差异。

## 当前状态

UI-15 保持 partial。现有 redraw scheduler 已限制普通 paint 到约 16ms，UI-15.1a 进一步减少进入 reducer 的
stream delta 数量；UI-15.2a/15.3a 已让默认 New UI 的 warm deep-scroll 和流式更新进入有界可见窗口渲染；UI-15.4a 已避免
超长文本正文驻留在前端状态；UI-15.6a 已建立 current renderer benchmark 基线；CC-02.1a/2.2a 已加入同合同
Ink 实验与核心视图对照。增量 virtual index、非文本 artifact、完整 cache revision、输入/resize/TUI benchmark
与达到语义 parity 后的跨前端性能门仍未完成。
