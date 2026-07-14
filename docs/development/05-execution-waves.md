# 后续开发实施波次

## Wave 0：文档与基线冻结

- 校验 33 个模块路径、依赖和状态；旧文档只作依据。
- 记录 main commit、协议版本、Store schema、UI capability、三平台基线。
- 阶段门：所有 planned 模块有 owner/领取模型和独立 commit 目标。

## Wave 1：边界、协议与安全回放

顺序：`ARC-01 → ARC-03 → HAR-05 → HAR-06`。

退出标准：Runtime 内部边界可测；协议可协商；Harness 可跨实例安全重放；Session 清理无孤儿。

## Wave 2：终端产品完成度

主线：`UI-10 → UI-11 → UI-12 → UI-13`；并行：`UI-14/15/16`；收口：`UI-17`。

每个 UI 模块一个 commit，必须同时更新 New UI 和 TUI fallback 的 capability/golden scenario。

## Wave 3：评测裁判与架构状态地基

顺序：`ARC-05 → HAR-08 → HAR-09`。

退出标准：baseline 可复现；重复失败形成 Proposal；迁移/备份/恢复可演练。

## Wave 4：源码对齐与 Renderer 决策

顺序：`CC-01 → CC-02`；根据 adopt/defer/reject 决策推进 `CC-03`；`CC-04/05` 可并行。

Ink 实验不得阻塞现有 UI 产品模块；如果 defer/reject，继续优化 current renderer。

## Wave 5：Runtime 服务化与执行隔离

顺序：`ARC-02 → ARC-04 → ARC-06 → HAR-10`。

退出标准：多客户端、worker、并发、心跳、取消、恢复和背压达到 A5；embedded fallback 保留。

## Wave 6：受控自进化

严格顺序：`EVO-01 → EVO-02 → EVO-03 → EVO-04 → EVO-05 → EVO-06`。

任何模块未 approved，不得提前开启下一道更高权限门。EVO-06 只能在至少一次真实 promotion +
rollback 演练后开始。

## Wave 7：闭源发布与可靠性

`ARC-07` 与 `ARC-08` 收口所有模块，执行三平台发布候选、升级/回滚、24h soak 和灾难演练。

## 提交格式

- `feat(harness): ... [HAR-05]`
- `feat(ui): ... [UI-10]`
- `refactor(runtime): ... [ARC-01]`
- `feat(evolution): ... [EVO-01]`
- `docs(development): ...`

模块内多 commit 只有在每个 commit 可独立验证且不暴露半成品公共接口时允许；最终交付必须有一个
汇总 commit/PR 映射到单一模块 ID。
