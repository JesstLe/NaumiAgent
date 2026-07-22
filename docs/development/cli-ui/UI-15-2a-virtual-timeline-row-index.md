# UI-15.2a 虚拟时间线行高索引

## 交付范围

本切片只解决默认 New UI 在长会话中向上深滚动时，渲染成本随 `scrollOffset` 线性增长的问题。
它不替换 renderer，也不提前完成 UI-15.3 cache revision、UI-15.5 scroll physics 或 CC-02 Ink 迁移。

## 实现合同

- 以 production message renderer 计算每张消息卡的真实行高，生成只包含
  `messageIndex/start/end/height` 的前缀索引；索引不保存 ANSI 正文副本。
- 深滚动通过二分查找定位首尾可见卡，只重绘可见卡及前后各一张 overscan 卡。
- 尾部与浅滚动继续走原有窗口化路径，避免为常见首帧强制扫描全部历史消息。
- 动态尾部（工作指示器等）不进入静态索引，每帧独立计算，避免把运行状态冻结在缓存中。
- message 数组身份/数量、终端宽度或 render-cache generation 变化时重建；fold、流式内容、活动状态等
  已有语义失效点统一通过 `clearRenderCache()` 推进 generation。
- anchor capture/restore 复用同一行高权威，不再另外保留全量 rendered lines。
- 欢迎页仅覆盖真正的空会话；若异步恢复内容先于 welcome dismiss 事件到达，时间线内容优先显示。

## 失败与边界处理

- 空消息、零行卡、超大 offset 与不存在的行均有确定返回值，不发生负索引或空视口。
- 缺失 state/render callback 时立即以中文错误拒绝构建，避免产生无效索引后静默错位。
- 提供 `disableVirtualTimeline` 诊断开关，用于同一状态下与旧路径逐行比对；它不是用户配置面。
- 语义失效依赖 central render-cache generation。绕过 reducer/clearRenderCache 原地修改消息的代码仍属于
  非法调用；后续 UI-15.3 会把 revision 字段显式纳入 key。

## 验证证据

发布 fixture 为 10,000 messages、1,000 tools、10MB 分页日志、120x40，fixture digest 与协议 digest
均和 CC-02.2a current baseline 一致。证据：
[UI-15-2a current release](evidence/UI-15-2a-current-release-darwin-arm64.json)。

| 指标 | CC-02.2a 历史 current baseline | UI-15.2a | 结果 |
|---|---:|---:|---|
| deep-scroll cold | 113.651ms | 118.415ms | 首次建索引约增加 4.2% |
| deep-scroll warm P95 | 92.746ms | 0.235ms | 下降约 99.7% |
| deep-scroll RSS delta | 177,192,960B | 69,812,224B | 本次采样下降约 60.6% |
| tail warm P95 | — | 0.344ms | 保持旧浅窗口路径 |
| paged-output warm P95 | — | 0.399ms | 保持旧浅窗口路径 |

历史基线只有 3 次 measured iterations，本次为 20 次，因此冷启动和 RSS 只作为同机趋势，不作为 CI
硬阈值；warm deep-scroll 的数量级变化同时由索引复用测试约束。

## 聚焦验收

- indexed 与 legacy deep viewport 逐行完全一致，包括 CJK、多行卡和 footer。
- warm render 复用索引；cache clear、宽度变化后必须重建。
- 二分查找覆盖空索引、零行卡、首尾边界和超大行号。
- 可见范围之外只允许前后各一张 overscan，索引调试信息明确 `storesRenderedLines=false`。
- release benchmark 所有场景固定输出 40 行且不超过 120 列。

## 后续依赖

下一步不应直接把整个 CC-02 做完。优先在 UI-15.3 建立显式 message semantic revision，消除索引和
render cache 对“调用者必须 clear”的隐式约束；随后 CC-02.3/2.4 的输入和滚动对照才能使用这个更新后的
current baseline，Ink 方案也必须提供等价的有界 projection，不能再用全量 O(n) 投影与旧 baseline 比较。
