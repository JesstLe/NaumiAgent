# UI-16.6a Working Indicator 运行反馈

## 问题

New UI 将旧的单行“运行中”提示升级为动态 working indicator 后，只渲染了结构化运行阶段，遗漏了
`runtime_status/perf_phase` 已写入的 `activeRuntimePhase`。因此“模型首包”等状态仍在 reducer 中，用户却看不到；
旧测试同时保留了已经废弃的文案，形成一项可复现的渲染回归。

## 契约

- 活动中的模型生成和工具执行显示“工作状态 · 运行阶段 · 最近性能阶段”；
- 性能阶段与运行阶段分别移除终端控制序列、压平空白并限制长度，最终仍按终端可见宽度截断；
- `TERM=dumb` 和 ASCII fallback 保留同等文字信息，不依赖动画或颜色；
- 等待权限、等待结构化用户输入与取消中的静态状态不显示性能阶段，避免把之前采样的指标误认为当前动作；
- run 完成、取消、错误或会话替换继续由既有 reducer 清空瞬态性能状态。

## 验收

- wide、compact、`TERM=dumb` 三种渲染都能看到“模型首包: 2400ms”；
- CJK 长文本和 OSC/CSI 控制序列不能突破宽度或形成终端注入；
- 权限、交互和取消等待态均不包含之前的“模型首包”；
- `runtime_status` 不复用或污染 `activeToolPrepare`；
- 只运行 working animation、相关 state/render 测试和 JavaScript syntax check，不以全量测试掩盖本切片结果。

## 未覆盖边界

本切片不实现 runtime status 高频事件合并、性能历史页、跨前端指标 parity 或完整终端矩阵；这些分别属于
UI-15.1、Runtime Inspector/UI-13、UI-17 与 UI-16 其余模块。
