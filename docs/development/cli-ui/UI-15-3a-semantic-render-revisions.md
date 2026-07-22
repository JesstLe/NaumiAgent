# UI-15.3a 语义渲染 Revision 与增量时间线更新

## 交付范围

本切片消除 UI-15.2a 对“任何消息变化都必须全局 `clearRenderCache()`”的隐式依赖。它为默认 New UI
建立有界的 semantic mutation authority，并让深滚动索引在 append 和单消息流式变化时增量更新。
这也是长周期 Harness 输出和 CC-02 性能对照可持续运行的前置，但不改变后端事件协议。

## 权威模型

- render cache 维护单调 `revision`、每消息 WeakMap revision 与最多 1,024 条 mutation journal。
- `markMessageRenderDirty(cache, message)` 同时推进消息 revision 和 journal；即使变化字段未出现在旧结构化
  cache key 中，也会产生确定性 cache miss。
- `clearRenderCache()` 仍是全局结构/主题失效权威：清空 LRU、推进 generation，并写入 `scope=all` 屏障。
- journal 被压缩或消费者落后于保留窗口时返回 `null`，时间线必须 fail closed 全量重建，不能猜测增量状态。
- journal 只保留消息对象弱 revision 与最多 1,024 条强引用；溢出后折叠为一个 global 屏障，不无界持有历史。

## 增量时间线

- 原数组尾部 append 只渲染新卡并扩展前缀段；前缀对象身份变化、删除、重排或 global 屏障仍全量重建。
- 多个尚未 paint 的同消息 mutation 按对象合并，一次重测真实卡片高度。
- 高度不变时只更新该段；高度变化时平移后续段的数值区间，不重新渲染后续卡片。
- assistant token/end、thinking delta/end、tool prepare/result 已接入单消息 revision；已有 run/permission/fold 等
  全局失效路径保持兼容。
- indexed viewport 始终与 `disableVirtualTimeline` legacy 路径做逐行语义等价验证。

## 可重复性能证据

运行：

```bash
cd frontend/terminal-ui
npm run benchmark:timeline-mutations -- \
  --output ../../docs/development/cli-ui/evidence/UI-15-3a-timeline-mutations-release-darwin-arm64.json
```

release fixture 为 10,000 messages、1,000 tools、120x40，先建立 deep-scroll 索引，再连续执行 100 次
真实 `handleAssistantStream(token) -> renderScreen()`：

- index build 1 次、append 1 次、partial update 100 次；
- render P50 0.253ms、P95 0.282ms、max 1.018ms；
- 不保存 rendered lines；最终 viewport 与 legacy 路径完全一致。

证据见 [UI-15.3a timeline mutations](evidence/UI-15-3a-timeline-mutations-release-darwin-arm64.json)。
单机时间只作为趋势证据；build/update 计数、bounded journal 和语义等价由确定性测试约束。

## 边界与未完成项

- 卡片高度变化目前需要 O(n) 平移后续数值段，但不再 O(n) 重新渲染；未来可换成 Fenwick tree，把点更新与
  prefix lookup 都收敛到 O(log n)。
- 任意位置 insert/delete/reorder 仍走 global rebuild；后续需要结构 mutation 类型和稳定 message identity。
- theme、颜色能力、完整 focus/selection revision 尚未形成独立 context token；UI-15.3 仍为 partial。
- Textual TUI 不使用该 Node render cache，因此本切片不虚构双端共用；TUI 性能对照仍属于 UI-15.6 后续。
