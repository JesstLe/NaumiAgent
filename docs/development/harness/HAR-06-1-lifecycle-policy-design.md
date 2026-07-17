# HAR-06.1 Session 生命周期策略设计

## 边界

本模块只定义 Session 与 Harness 派生数据共同使用的策略判断，不写数据库、不删除文件，也不
启动 retention worker。HAR-06.2 reconciliation、HAR-06.3 tombstone、HAR-06.4 Artifact GC
必须消费此处的同一决策，不能各自复制规则。

## 策略语义

- `retain`：保留 Session 与全部派生记录，不允许自动降级或清理。
- `archive`：从默认历史列表隐藏，但保留 Harness 解释与证据；只有 retention worker 在满足
  后续保留条件时才能请求转为 `delete`。
- `delete`：进入待协调删除状态；仅该策略允许自动清理派生记录。
- `legal_hold`：最高保护级别，阻止一切自动转换与清理。

## 操作者

- `user`：可明确请求任意普通转换。进入或退出 `legal_hold` 必须提供非空审计说明。
- `retention_worker`：只允许把 `archive` 转为 `delete`；天数、空间上限等资格由 HAR-06.5
  在调用策略前验证。
- `system_recovery`：只能重放已经处于目标状态的幂等请求，不能改变策略。

同状态请求为幂等成功。`legal_hold` 退出可以直接选择目标策略，但必须由用户明确操作并留下
审计说明；这比隐式“先 retain 再 delete”更能保留真实意图，也便于后续写一条原子审计记录。

## 验收标准

- 所有策略和操作者均为封闭枚举，未知持久化值稳定失败。
- 现有 Session Store 的 `active/archived` 通过单一适配器映射为 `retain/archive`，未知状态失败关闭。
- `legal_hold` 对 worker 和 recovery 始终不可穿透。
- `retain` 不会被 retention worker 自动归档或删除。
- `archive -> delete` 是唯一允许改变状态的自动转换。
- 进入/退出 legal hold 缺少审计说明时拒绝。
- 决策对象明确暴露 allowed、idempotent、requires_audit 和 automatic_cleanup_allowed。
