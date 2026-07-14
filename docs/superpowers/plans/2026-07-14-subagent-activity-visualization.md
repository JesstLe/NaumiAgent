# 子智能体活动可视化实施计划

> 对应设计：`docs/superpowers/specs/2026-07-14-subagent-activity-visualization-design.md`

## 任务 1：补齐 typed UI message

1. 先扩展 Adapter 测试，固定 description、tokens、cost、timestamp。
2. 扩展 `SubagentEventMessage` 与 Adapter。
3. 更新 TUI renderer 的中文状态与详情。
4. 只运行 Adapter/TUI renderer 测试。

## 任务 2：New UI 状态聚合

1. 先写同 task 聚合、终态后新一轮、事件上限测试。
2. 实现 `subagent_activity` 状态模型与 reducer。
3. 把卡片加入 fold registry。
4. 只运行 state 测试。

## 任务 3：New UI 可折叠卡片

1. 先写折叠/展开、状态颜色、资源与窄终端测试。
2. 新增 `subagent-card.js` 并在 Message 分发。
3. 移除 `subagent_event` 对通用 EventCard 的依赖。
4. 只运行 components/render 测试。

## 任务 4：验证与提交

1. 跑相关 Python 与 Node 小模块测试。
2. Ruff、py_compile、node --check、git diff --check。
3. 审视事件字段、终态、ID 复用、折叠发现和跨平台终端宽度。
4. 以英文独立提交。
