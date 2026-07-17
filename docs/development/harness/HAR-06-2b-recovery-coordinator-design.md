# HAR-06.2b / HAR-06.3b Session 恢复协调器设计

## 目标

把 HAR-06.2a reconciliation 状态与 HAR-06.3a tombstone 真正组合为可执行、可恢复的删除流程，
同时保持 Harness 不反向依赖 AgentEngine。协调器只消费最小 Session `load/delete` 协议。

## 直接执行

1. 读取 Session，以保存的 workspace 和 created_at 生成确定性 request id。
2. 用 HAR-06.1 policy 判断用户 `delete` 转换；未知状态失败关闭。
3. `prepared` 持久化成功后才调用 Session delete。
4. Session 不存在是提交事实；若 delete 返回 false，则再次 load 判定并发删除或真实失败。
5. 推进 `session_committed` 后，原子清理 Harness 行并推进 `records_committed`。
6. 任一阶段失败只写封闭错误码 tombstone，返回 `retry_scheduled`，不泄露异常原文。

## 恢复执行

- 启动扫描有硬上限的未完成 reconciliation；若崩溃发生在写 tombstone 前，以稳定 discovery
  failure id 补建 tombstone。
- worker 原子领取到期或租约过期 tombstone，再从持久 reconciliation state 继续，而不是重头猜测。
- tombstone 一旦存在，用户的直接重试只返回当前调度状态，不绕过租约执行，避免成功后遗留 pending。
- `prepared` 时 Session Store 是否存在是权威：存在则删除，不存在则直接确认提交。
- `session_committed` 只执行 Harness 数据库协调；`records_committed` 只解决 tombstone。
- 成功只能由当前 lease owner 标记 resolved；失败以新的稳定 failure id 增加 attempt 并释放租约。

## 结果契约

协调器只返回 `completed | retry_scheduled | not_found | policy_blocked`、request/session id、持久状态
和 tombstone 状态。异常原文只存在于进程日志（由调用方按安全策略处理），不进入结果或数据库。

## 当前接入边界

本切片完成协调器与真实故障恢复，但仍不替换 `/delete`。Engine 接入必须同步活动会话清空、权限
授权撤销、CLI/TUI 回执与启动恢复生命周期，并在独立切片完成，避免出现“后台已完成但 UI 报失败”。

## 验收标准

- request id 对同一 workspace/session/created_at 稳定，对不同 Session 实例不同。
- 正常路径真实删除 Session 与精确 Harness 行。
- Session delete 异常产生 `session_store_error`，修复后由新实例恢复完成。
- Harness 清理异常保留 `session_committed`，修复后不再次删除 Session。
- 崩溃留下但无 tombstone 的 reconciliation 可被发现并纳入重试。
- policy 未知时不创建 reconciliation、不删除任何数据。
- 所有恢复查询、领取和批处理有硬上限；一个任务失败不伪造其他任务成功。
