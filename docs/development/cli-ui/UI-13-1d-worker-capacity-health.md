# UI-13.1d Worker Capacity Health

## 1. 目标

把 ARC-06.1a/1b 的 Worker capacity reservation authority 投影到共享 Doctor Health，使用户在 New UI 与
Textual fallback 中看到每个 active Worker 的合同上限、当前权威占用和可用槽位。本切片只读，不派发 Job、
不收割 TTL、不修改 Registry，也不把 reservation 数量冒充 OS 进程或模型实际负载。

## 2. 权威读取

`inspect_worker_authority_health()` 在同一个 SQLite `mode=ro + query_only` 读事务中组合：

- 已验证摘要和索引列的 active Worker contract；
- 同一 `worker_id + instance_id + epoch` 下状态为 active 的 reservation；
- Doctor `assessed_at` 时刻尚未到期的 reservation 数量；
- Harness 中匹配同一 incarnation 的 heartbeat。

每条 reservation 复用 Worker Registry 的公共反序列化验证，不在 Doctor 内复制 schema。instance/epoch 不匹配、
终态字段矛盾、非法时间、超过合同容量或数据库损坏均使 Registry 诊断 fail closed。

TTL 已到但尚未被写路径收割的 active 行在只读快照中不占槽位；Doctor 不把该行改写为 `expired`，因此诊断前后
数据库字节保持一致。`bypass` 不改变可见容量或计算规则。

## 3. 双端用户体验

共享中文摘要使用：`容量占用 reserved/maximum、可用 available`，并继续显示 Worker kind、epoch、平台和心跳。

- New UI 通过既有 `doctor/health` schema v1 runtime item 显示，沿用正常/受限/错误/未知色彩与文字标签；
- Textual fallback 通过同一个 `DoctorCheck` Markdown 路径显示相同事实；
- 无 active Worker 时仍显示正常的空注册中心状态；
- 容量已满本身不是产品故障，heartbeat/identity/Store 健康仍独立决定 severity；
- detail 不包含 job id、reservation id、绝对数据库路径、secret 或原始异常。

## 4. 聚焦验收

- 真实 Registry capacity=4、active reservation=1 显示 `占用 1/4、可用 3`；
- 已过期但尚未写回的 reservation 显示 `占用 0/4、可用 4`，Registry bytes 和行状态不变；
- 篡改 reservation instance 后只读检查返回稳定 `registry_unreadable`，不自动修复；
- typed Doctor item 仍归入 runtime domain，New UI 在 80/120/200 列完整呈现容量文字；
- Textual Markdown fallback 包含同一容量摘要；
- 只运行 Worker Registry/Authority、Doctor Health 与 Doctor 页面组件小模块测试，不运行全量测试。

## 5. 明确未完成

- reservation 表示 Runtime 已承诺的 slot，不证明 worker 内真实 CPU、内存、进程或队列深度；
- 尚无 queue wait、reservation age、TTL orphan、利用率历史或容量趋势，它们属于 ARC-06.8/ARC-08；
- 尚无 Agent/Browser 持久 Worker capacity producer；Doctor 只展示真实存在的 authority，不生成模拟数据；
- 多 Worker 聚合、筛选和详情页需要独立 bounded typed contract，不能继续扩张一条 Doctor detail 字符串。
