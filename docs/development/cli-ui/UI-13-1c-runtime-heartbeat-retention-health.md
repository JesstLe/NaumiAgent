# UI-13.1c Runtime Heartbeat Retention Health

## 1. 目标

把 HAR-10.2f2 的 runtime heartbeat retention typed status 投影到 Doctor Health，让用户能直接区分策略关闭、
正常等待、跨实例 standby、未运行、本轮失败和当前客户端不可观测。本切片只读，不在打开 Doctor 时启动、停止、
wake 或执行清理。

## 2. 权威输入与状态映射

New UI 使用 Bridge 已有的 `runtime_heartbeat_retention` 状态：configured、state、成功周期、累计删除、失败次数、
稳定错误码和最后周期时间。投影固定使用 `runtime-heartbeat-retention` item ID，归入 runtime domain：

- 显式关闭：`ok`，说明不会自动删除诊断记录；
- `running/waiting/standby`：`ok`；standby 表示另一实例持有独立租约，不是故障；
- configured 但 `stopped`：`degraded`；
- `failed`：`degraded`，因为诊断清理受限但模型执行仍可继续；
- New UI 与 TUI 均从各自共享 lifecycle snapshot 读取实时状态；缺少 Composition 注入时为 `unknown`；
- 未知或非法状态：`unknown`，不猜测健康。

计数机械转为非负整数；错误码只允许有界小写标识符。raw exception、Store 路径、secret、heartbeat detail、用户正文
和 reasoning 都不会进入 Doctor payload。普通输出仍经过 OutputGuardrail。

## 3. New UI 与 TUI

- New UI 将该 item 与本地 Doctor、Bridge heartbeat、Worker authority、Pursuit recovery 放入同一 typed 页面；
- 现有 severity 颜色继续区分正常、受限、错误和未知，同时保留完整文字标签，不依赖颜色表达；
- TUI fallback 复用同一个 `DoctorHealthItem` 与 Markdown renderer，并已消费 Composition Root 提供的 terminal
  lifecycle factory；
- TUI 不会为了让界面“变绿”而创建第二个调度器，也不会读取或修改 Bridge 私有内存状态；
- Doctor 失败 fallback 仍保留 retention item，且底层异常正文不外泄。

## 4. 验收证据

- waiting、failed、unavailable 与非法输入得到确定性 severity、中文详情和建议；
- 非法计数和 secret-shaped 错误码不能进入公开 item；
- Bridge `/doctor` 同时包含 retention 与 Pursuit recovery item；
- TUI fallback Markdown 明确显示 `UNKNOWN` 和不可观测原因；
- New UI 在 80/120/200 列宽正确渲染 runtime item、产品归因和下一步，且所有行不越界；
- 只运行 Doctor Health、两个 Bridge Doctor 节点、一个 TUI 节点与 Doctor 页面 Node 测试，不运行全量测试。

## 5. 当前不足与下一步

ARC-01.4c1-4c3 已建立共享 terminal lifecycle 并迁移 New UI/TUI；两端都能提供真实 heartbeat/retention 状态与关闭
语义，且没有复制 producer。尚未完成的是 UI-17 golden parity fixture、手动 wake、历史清理详情和 SLO 趋势；后三者
分别属于控制面与 ARC-08，不应塞入只读 Doctor 投影。
