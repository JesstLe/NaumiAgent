# HAR-10.2f1 Runtime Heartbeat Retention Periodic Core

## 目标

把 HAR-10.2d 的并发安全清理 authority 与 HAR-10.2e 的 typed runtime catalog 组合为可独立验证的周期服务。
本切片只交付调度核心，不接入 New UI Bridge，也不默认启动后台删除；这样配置、生命周期和用户可见状态可以在
HAR-10.2f2 中作为独立功能验收。

## 周期合同

`RuntimeHeartbeatRetentionService` 的每轮顺序固定为：

1. 以稳定 run ID `runtime-heartbeat-retention` 获取 `HarnessRunKind.RUNTIME` 独立租约；
2. 使用同一 `assessed_at` 读取有界 runtime catalog；
3. 合并调用方声明的当前 subject，以及 `starting/healthy/draining/stale/clock_regression` 项形成保护集合；
4. 在删除边界前按原 owner、epoch 精确续租，续租失败则不调用 prune；
5. 调用 HAR-10.2d 的有界、原子 prune，由 Store 在写事务内再次评估候选健康状态；
6. 在 `finally` 中按原 owner、epoch 精确释放租约。

服务在两轮之间不持有租约。另一个进程拿不到租约时进入 `standby` 并短周期重试，不会把竞争当成故障，也不会执行
删除。此租约不得复用 Session retention 的 lease，因为两者的工作负载、故障域和启停生命周期不同。

## 安全边界

- 默认保留期为 7 天，最短 3 天、最长 1 年；最短值高于 heartbeat 最大 offline 安全窗口；
- 每轮 catalog 最多读取 200 项、prune 最多扫描 100 项，调用方边界分别为 200 和 1000；
- catalog 是保护和观测输入，不是删除裁判；即使活跃项不在首个 catalog page，Store prune 仍在事务内重新分类，
  只删除 old `offline/stopped/failed` runtime；
- `stale` 和 `clock_regression` fail closed，不能仅凭疑似异常时钟删除；
- 时钟、Store、catalog、prune 或 lease 异常只公开稳定错误码，不泄露数据库路径、异常文本或 secret；
- `start()`、`stop()` 幂等，停止会唤醒 interval/standby 等待，但不会粗暴取消正在提交的 Store 事务；
- runtime producer 与本服务互不依赖，清理失败不得中断 heartbeat 写入。

## 状态与回执

`RuntimeHeartbeatRetentionSnapshot` 只暴露：

- `stopped/running/standby/waiting/failed` 状态；
- 成功周期数、累计删除数、失败数；
- 稳定 `last_error_code`、最后周期时间和下一等待时间。

删除明细继续使用 HAR-10.2d 的 `RuntimeHeartbeatPruneReceipt`，不另造第二套回执。周期成功后清空旧错误码；获取租约
失败、运行失败、租约丢失与释放失败保持可区分。

## 验收证据

- 真实 Harness SQLite 中删除超过保留期的 offline runtime，并保留当前 healthy runtime；
- 删除前续租丢失时，prune 从未被调用，原 epoch lease 仍在 `finally` 释放；
- 无租约实例进入 standby，不产生删除；
- 原始 Store 异常和无时区时钟只形成稳定错误码；
- background start/stop 幂等，并可立即打断 60 秒等待；
- 与 HAR-10.2d/10.2e 定向组合测试通过，不运行全量测试。

## 未完成与下一步

HAR-10.2f2 已完成安全默认配置、Bridge 启停、当前 subject 保护和 typed runtime status；见
[设计](HAR-10-2f2-runtime-retention-bridge-lifecycle.md)。New UI/Doctor 的专用详情投影仍应作为独立只读切片完成。
