# UI-17.2a Terminal Runtime Health Golden Scenarios

## 1. 目标

建立 UI-17.2 第一组阻断级双端行为对照：New UI Bridge 与 Textual TUI 必须从同一 terminal lifecycle 事实生成完全相同的
runtime heartbeat retention 公开语义，Node New UI 必须原样消费该有界 payload。此切片消费 ARC-01.4c1-4c3 和
UI-17.1，不提前覆盖 permission、interaction、tool 或 receipt。

## 2. Golden 合同

权威 fixture：`tests/fixtures/ui17/runtime-heartbeat-retention-golden.json`。schema 1 固定四个场景：

- Composition factory 缺失：`unavailable`，不能伪装为 worker 已停止；
- 策略关闭：`stopped` 且 configured=false；
- 正常等待：保留周期、删除数、失败数、时间和下一延迟；
- 失败且输入越界：负数归零、计数限制为 JavaScript safe integer、非法错误码变为 `status_invalid`、非法时间清空、
  非法延迟归零。

每个公开 payload 必须恰好包含 configured/state/cycle/deleted/failure/error/time/delay 八个字段。Python shared projector
是唯一边界清洗实现；Bridge/TUI 只提供 configured、available 和 typed snapshot，不能各自复制 dict 规则。

## 3. 真实表面验证

- 纯 projector 对四个 fixture 逐字段相等；
- 实际 `JsonlEngineBridge` 与 `NaumiApp` adapter 对同一 fixture 逐字段相等；
- 两端生成的 `DoctorHealthItem` 完全相等；
- Node `reduceServerEvent(runtime/status)` 消费同一 fixture，`state.status.runtime_heartbeat_retention` 不丢字段、不改值；
- 既有 Bridge retention、startup degradation、TUI live Doctor 和 Doctor health 页面测试保持通过；
- Python 20 项与 Node 3 项定向回归通过，Ruff、compile、JSON 与 diff check 通过，未运行全量测试。

## 4. 自我审视与下一步

本切片证明的是 runtime-health 一项能力的跨语言、跨 adapter 语义一致，不代表 UI-17.2 全部完成。下一独立切片应选择
permission/bypass 与 model-initiated interaction 这组安全关键控制事件，使用同一 fixture 验证 request、choice、terminal
resolution 与取消字段；随后再覆盖 submit/tool/receipt/cancel。视觉布局差异不进入语义 fixture，但两端必须保留文字标签，
不能只靠颜色表达状态。
