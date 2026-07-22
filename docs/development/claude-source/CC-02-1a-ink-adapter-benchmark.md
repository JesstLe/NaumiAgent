# CC-02.1a Ink 实验 Renderer 与同合同 Benchmark

## 决策

当前结论：`defer`，不替换默认 New UI。

本切片首次建立可执行的 React/Ink 6 renderer adapter，并让它与 production current renderer 消费
同一 state、生命周期 fixture、固定视口和 `naumi.renderer-benchmark.v1` 合同。它证明 Ink 可以接入，
但也用实测否定了“现在即可迁移”的结论。

## 为什么不是 Brainless

2026-07-23 复核 `theswerd/brainless`，上游仍为 `main@4c5d5ab`。它的 Next.js、React DOM、shadcn、
Radix、Tailwind 与 Shiki 组件适合作为视觉信息架构和截图方法参考，但不能处理 terminal cell width、
ANSI、PTY resize、raw input、IME 或信号。CC-02 因此不引入 Brainless 源码或依赖；真正的终端候选仍为
Ink。

## 实现

- `src/experiments/ink-renderer.js`：真实调用 Ink `renderToString()`，从 production `state.messages`、
  mode、running、scrollOffset 和 composer 构造固定宽高 frame；覆盖 user、assistant、activity、tool、
  run activity、completion receipt 和 permission 的实验性单行语义。
- `src/experiments/ink-golden-capture.js`：复用 UI-17.2f lifecycle fixture 和 anchor，不维护 Ink 专属
  优惠样本；输出相同 golden-frame schema 与 digest。
- `scripts/benchmark-renderer.js`：current runner 保持默认行为，同时开放严格 renderer adapter 注入点；
  fixture 生成、digest、协议版本/registry digest、三场景、计时、RSS 与 viewport 判定只有一份 authority。
- `scripts/benchmark-ink-renderer.js`：输出 renderer=`react-ink-6` 的同合同结果。
- Ink 6.8.0 与 React 19.2.8 仅为 `devDependencies`。没有进入默认 New UI runtime；选择 Ink 6 是为了
  保持项目 Node >=20.10 基线，Ink 7.1.1 目前要求 Node >=22。

## 同机 release 证据

环境：Darwin arm64、Node v24.12.0；fixture digest 均为
`3ecbea6d66ec57a1fbbf1359f1961c4ff70c816ce75f0375dbf038240d0ceba5`；10,000 条消息、1,000 个工具、
10MB 分页输出、120×40，3 次采样且不预热。

| 场景 | current P95 | Ink P95 | 结论 |
|---|---:|---:|---|
| tail | 0.913ms | 8.379ms | Ink 约慢 9.2 倍，未通过 15% 门槛 |
| deep_scroll | 89.809ms | 3.914ms | Ink 实验 adapter 的直接 viewport slice 明显更快 |
| paged_output | 0.326ms | 6.699ms | Ink 约慢 20.5 倍，未通过 15% 门槛 |

证据：

- [current release JSON](../cli-ui/evidence/CC-02-1a-current-release-darwin-arm64.json)
- [Ink release JSON](../cli-ui/evidence/CC-02-1a-ink-release-darwin-arm64.json)

deep-scroll 的优势不能外推为整体优势：当前 Ink adapter 只渲染已切片的单行代表项，尚未承担 current
renderer 的完整卡片高度、fold、Markdown、diff、permission modal 和页面状态。相反，tail/paged 的
明显退化已经足以阻止当前迁移。

> 后续更正：CC-02.2a 发现本切片的超大 deep-scroll offset 可落到空视口边界，因此这组 deep-scroll
> 数值只保留为历史证据，不再用于迁移判断。CC-02.2a 已钳制到最早可见内容并重新生成成对结果。

## 验收证据

- current 与 Ink 的 fixture 对象、SHA-256、协议版本和 registry SHA-256 完全相同；场景均为
  tail/deep_scroll/paged_output。
- Ink frame 固定为请求行数，所有行可见宽度不超过终端宽度，并且 renderer 不修改输入 state。
- UI-17.2f 的“执行定向验证 / bash_run / 定向验证通过”三个共享锚点全部出现且重复捕获 digest 稳定。
- dependency audit 为 0 个已知漏洞；JavaScript syntax check 覆盖实验文件。

## 未完成与下一门

CC-02.1 仍为 partial，CC-02 整体也仍为 partial：

- 五个代表 view 尚未达到 production component parity；当前只完成共享 adapter 与代表性 timeline；
- multiline、IME、paste、key parsing、permission modal 优先级尚未接入；
- token burst、resize、输入延迟、真实 PTY、Windows/Linux 以及安装/离线体积尚未测量；
- 当前捕获是 `renderToString()` 的 deterministic frame，不等同于交互式 Ink runtime；
- 未产出 adopt/reject 终局，只能 `defer`。下一切片应先做 CC-02.2 production semantic view parity，
  再重复同合同 benchmark，不能因为 deep-scroll 单点胜出而提前迁移。
