# CC-02.4a Ink 有界 Presentation Row Index

## 1. 交付结论

本切片消除 CC-02.2a Ink 实验 renderer 在每一帧对全部消息执行 `flatMap(presentMessageRows)` 的
O(n) warm projection。Ink 现在复用 UI-15.2/15.3 的行高索引、二分定位和 mutation journal，只投影
当前 viewport 涉及的消息及前后各一条 overscan。

默认 New UI 没有切换到 Ink，采用结论继续是 `defer`。

## 2. 多 Renderer 索引隔离

原 `timeline-row-index` 以 state 为唯一 WeakMap key，若 current 与 Ink 在同一 state/width 上交替渲染，
不同 presentation 行高可能错误复用。现在索引增加显式 namespace：

- current renderer 默认使用 `current`；
- Ink 使用 `ink-presentation-v1`；
- 每个 namespace 独立保存 generation、revision、segments、统计和 viewport range；
- namespace 为空或超过 128 字符时失败关闭；
- WeakMap 下只存在固定少量 renderer namespace，不把 rendered text 存入索引。

因此共享 state 不再意味着共享错误的行高权威。

## 3. 有界投影

Ink 首次渲染仍需扫描消息计算 presentation height；后续相同 width/generation：

1. 重读 render-cache mutation journal；
2. append 只追加新 segment；
3. 单消息变化只重算该消息高度；
4. 根据 `scrollOffset` 计算 viewport start/end；
5. 二分定位首尾 segment；
6. 只对可见 segment 与一条 overscan 调用 `presentMessageRows()`；
7. 在消息边界处按 local row range 精确裁剪。

保留 `disablePresentationIndex` 诊断路径，以同 state 逐行比较 legacy O(n) projection；它不是用户设置。

## 4. 语义与安全验收

- 300 条消息、40 个工具和分页输出的深滚动 indexed/legacy frame 逐行完全相同；
- 第二次 indexed render 不重建，reuse count 增长；
- visible range 小于 20 个 segment，索引明确不保存 rendered lines；
- current 与 Ink namespace 在同 state 上分别得到 1 行/2 行高度，不交叉污染；
- permission、task、footer 与 terminal lifecycle 的既有共享语义锚点继续通过；
- render 前后 production state JSON 完全相同。

## 5. 同机 Smoke Benchmark

Darwin arm64、Node v24.12.0，1,000 messages、100 tools、100k 分页日志、120×40、12 次采样；三份
证据使用相同 fixture SHA：
`873128ca669ccb61de38fa50909c67d9dbc9610bb1af88bbcbcf630edc9d6105`。

| 场景 | current P95 | Ink legacy P95 | Ink indexed P95 | indexed 相对 legacy |
| --- | ---: | ---: | ---: | ---: |
| tail | 1.365ms | 9.769ms | 8.480ms | -13.2% |
| deep_scroll | 1.740ms | 5.322ms | 4.265ms | -19.9% |
| paged_output | 1.765ms | 7.132ms | 6.779ms | -5.0% |

证据：

- [current smoke](../cli-ui/evidence/CC-02-4a-current-smoke-darwin-arm64.json)
- [Ink legacy smoke](../cli-ui/evidence/CC-02-4a-ink-legacy-smoke-darwin-arm64.json)
- [Ink indexed smoke](../cli-ui/evidence/CC-02-4a-ink-indexed-smoke-darwin-arm64.json)

索引确实降低 warm projection 成本，但 Ink P95 仍约为 current 的 2.5-6.2 倍，未满足“不退化超过
15%”的采用门。Smoke 时间是单机趋势证据，不是跨平台 release SLO。

## 6. 自我审视与剩余边界

- cold render 仍需 O(n) 建高索引；任意位置 splice/reorder 仍会重建。
- segment 高度变化仍需 O(n) 平移后续数值区间，尚未采用 Fenwick tree。
- React/Ink `renderToString()` 本身仍是主要 warm 成本，presentation index 不能掩盖这一事实。
- 输入、IME、permission focus、resize、真实 PTY 与三平台安装体积尚未完成。
- 当前只是确定性实验 renderer；不得据此替换默认 New UI、删除 current renderer 或扩大运行时依赖。
