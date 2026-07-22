# UI-15.6a Current Renderer 可重复性能基线

## 目的

为 UI-15 后续虚拟化、分页和缓存优化建立同一台机器上可重复比较的 JSON 基线，并为 CC-02
React/Ink 可替换性实验提供 current renderer 对照组。本切片测量事实，不把单台开发机数值写成
跨平台发布阈值。

## 运行方式

```bash
cd frontend/terminal-ui
npm run benchmark:renderer -- --profile smoke
npm run benchmark:renderer -- --profile release --output /tmp/naumi-renderer-release.json
```

`smoke` 使用 1,000 条对话、100 张工具卡和 100KB 日志，适合本地回归；`release` 使用验收规模的
10,000 条对话、1,000 张工具卡和 10MB 日志。两者都测量 tail、deep_scroll、large_output 三个
场景，并分别输出首次冷渲染、warm min/P50/P95/max、RSS 增量、视口行数与宽度边界。

## 可比较合同

- 输出 schema 固定为 `naumi.renderer-benchmark.v1`，renderer 标识为 `current-node`。
- fixture generator 版本、消息数、工具数、日志字符数、宽高进入 SHA-256；后续 Ink runner 必须
  复用相同字段与内容生成规则，禁止用更轻 fixture 制造优势。
- Node 版本、平台和架构写入 runtime；跨机器结果不能直接混作性能回归。
- 计时使用单调高精度时钟，首次冷渲染单列，warmup 不进入 warm 样本；P95 按有界样本的
  nearest-rank 计算，避免缓存预热掩盖首次大输出成本。
- 每个场景必须输出固定视口行数并验证可见宽度，性能快但越界的 renderer 不算通过。

## 验收证据

- 单元测试以小型真实 state 覆盖三场景、输出 schema、digest、百分位关系、视口边界和非法参数。
- 本地真实 `smoke` 运行成功并产生三组指标；脚本不依赖 shell 或平台专属命令。
- 2026-07-22 的 Darwin arm64 / Node 24 release 规模实测已落盘到
  `evidence/UI-15-6a-current-renderer-release-darwin-arm64.json`。它暴露出 deep-scroll P95 约
  约 104ms、10MB large-output 冷渲染约 1.05s 和约 355MiB RSS 增量；这些是后续虚拟化与
  Artifact paging 的 RED 基线，不是可接受发布阈值。
- runner 直接调用生产 `createInitialState()` 与 `renderScreen()`，不是另写简化 renderer。

## 自我审视与剩余边界

- 本切片只建立 current renderer benchmark runner；尚未定义 CI 阈值，也没有宣称 UI-15.6 完成。
- RSS 是进程级观测，受 GC 与运行时影响，应以同机多轮趋势使用，不作为单次绝对判定。
- 输入延迟、token 1000 events/s、连续 resize 和 Textual TUI 基线仍需 UI-15.6b+ 单独实现。
- CC-02 仍必须实现 Ink 同 fixture runner 后才能形成 adopt/defer/reject 决策。
