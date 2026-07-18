# HAR-10.1b Pursuit Lease/Fencing 接入

## 目标

让生产运行时中的每个 Pursuit executor 在规划、工具执行和终态提交前持有 HAR-10.1a 权威租约。两个进程
不得同时把同一 Pursuit run 当作自己的运行；租约丢失后旧 executor 必须 fail closed，不能继续提交结果。

## 运行时装配

`AgentEngine` 将 Composition Root 已拥有的 `HarnessStore` 与规范化 `workspace_root` 注入
`set_pursuit_dependencies()`。`PursueTool` 创建的每个独立 `GoalPursuitLoop` 继续传递同一 lease port，不创建
第二个 Store 或临时 lease 协议。

无 Harness lease port 的 `GoalPursuitLoop` 仅保留给隔离单元测试和显式嵌入场景；默认 Naumi Runtime 路径
始终启用 lease。

## Admission 与启动回执

新 run ID 使用 24 位随机十六进制标识，不再使用秒级时间戳，避免同一秒启动两个目标时覆盖记录。

启动顺序固定为：

1. 在内存创建 run；
2. 获取 workspace + `pursuit` + run_id 的 Harness lease；
3. 记录 `run-start` fencing receipt；
4. 保存初始 PursuitRun；
5. 触发 startup acknowledgement；
6. `PursueTool` 才向用户回报后台启动和 run_id。

lease DB 故障、竞争失败、fencing 拒绝或 10 秒 admission 超时都返回“未启动”；超时任务会取消，不能继续在
用户看不到的后台幽灵运行。

## Keepalive 与安全边界

`PursuitLeaseSession` 默认 lease 为 300 秒、每 100 秒续租；续租只接受精确 owner/epoch。续租异常、过期、
接管或 fence 拒绝会设置不可逆 lost 状态。

下列边界记录 immutable fence receipt：

- run start、parse result、worktree start/result；
- 每轮开始、assessment、plan result；
- recovery plan/action result；
- 普通 action start/result；
- background task start/result/follow-up；
- verification start/result；
- waiting 与所有 terminal transition。

执行适配器必须重新抛出 `PursuitLeaseLostError`，不能把它转换成普通 tool error 后继续下一轮。同步
`PursuitStore.save_run()` 也会检查本地 lease session 的 lost/closed 状态，阻止已知失租后的写入。

## 终态与恢复语义

- `completed` 只在强制 final verification 真正通过后持久化；候选 completion 不再提前污染权威状态；
- cancel 只设置请求标记，由持有 lease 的循环在下一安全边界提交 `cancelled`；
- waiting/blocked/terminal 完成后停止 keepalive，并以精确 epoch 释放 lease；释放行保留以维持 epoch 单调；
- `/pursue resume` 先竞争同一 run lease，live owner 存在时不读取后台结果、不改写 PursuitStore；
- HAR-10.4a 已新增 GoalSpec、criteria、budget、todo、history、evidence cursor 和等待项 checkpoint。旧记录
  仍进入 `blocked/checkpoint_required`；新记录通过完整性校验后进入 `blocked/checkpoint_ready`。HAR-10.4b
  resume executor 接入前，两者都不会错误显示为 `running`。

## 验收证据

- 真实 Harness/Pursuit SQLite 中，完整 loop 的 run-start 与 terminal-blocked fence 均为 accepted/current；
- loop 完成后 lease 为 released、epoch 为 1，重新接管时 epoch 单调增加；
- 过期接管后旧 session 的 late result 被拒绝，执行适配器不会吞掉 lease-loss 异常；
- live owner 存在时 resume 返回 owner/expiry，PursuitRun 字节语义不变；
- lease Store 报错时 `/pursue` 返回未启动且 PursuitStore 没有幽灵 run；
- final verification 失败后最终状态为 blocked，任何时刻均不持久化 completed；
- 既有 Pursuit 单元模块继续通过，默认 Engine 装配确认使用 Composition Root HarnessStore。

## 当前不足

HAR-10.1b 保护执行准入、续租、状态/结果提交边界，但不能撤销租约丢失前已经发往外部系统的 in-flight
副作用。background/browser/shell 等执行器仍需 HAR-10.3/10.5 的 durable idempotency key、取消传播和
reconcile；Pursuit 的真正跨进程继续执行依赖 HAR-10.4b resume executor。HarnessStore 与 PursuitStore 是两个
SQLite DB，当前不是跨库原子事务；本切片通过提交前 fencing 和 lost-state fail closed 缩小窗口，但不声称
已解决跨 Store 原子性。
