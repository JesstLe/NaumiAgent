# HAR-06.3a 协调 Tombstone 与重试租约设计

## 目标

当 Session/Harness 协调失败或进程退出时，保留不含敏感正文的恢复事实，并允许多个进程安全竞争
重试任务。本模块只负责 durable retry control plane；HAR-06.2b 才执行具体协调步骤。

## Tombstone 内容

- request id、固定策略 `delete`
- 失败阶段：`session_delete | harness_records`
- 封闭错误码：`session_store_error | harness_store_error | cancelled | infrastructure_error`
- 状态：`pending | leased | exhausted | resolved`
- attempts、max attempts、next retry、lease owner/expiry、创建和更新时间

禁止保存异常原文、objective、消息、命令、模型输出或 Evidence summary。每次失败只额外保存
`failure_id + request_id + occurred_at` 作为幂等事件；同一 failure id 重放不会重复增加 attempts。

## 调度规则

- 退避为确定性的指数退避并带 request 级稳定 jitter，最大一小时；重启后不会改变。
- attempts 达到 max attempts 后进入 `exhausted`，不再被 worker 领取。
- worker 在单一 SQLite 写事务中选择到期 tombstone 并写入租约；并发 worker 不会同时获得同一项。
- 租约到期后其他 worker 可接管；未到期时不可抢占。
- 租约有效区间统一为 `[claimed_at, expires_at)`；边界时刻只允许接管方，不接受旧 worker 回执。
- 只有当前 lease owner 能报告下一次失败或标记 resolved；旧 worker 结果失败关闭。

## 状态关系

Tombstone 必须引用已存在的 reconciliation request。成功解析不会删除 tombstone 或失败事件，
以保留最小审计轨迹；后续 retention 只能按明确的审计保留策略清理。

## 验收标准

- Harness DB v3 可加法迁移到 v4，旧协调记录不丢失。
- 同一 failure id 幂等，不同 failure id 才增加 attempts。
- 两个 Store/worker 并发领取时只有一个成功。
- 租约未过期不可抢占，过期后可恢复。
- exhausted 不再调度；resolved 只能由租约持有者完成并可幂等重放。
- 损坏枚举、未知错误码和错误 lease owner 均失败关闭。
- 真实数据库跨 Store 实例完成失败、领取、再次失败、重新领取和解决。
