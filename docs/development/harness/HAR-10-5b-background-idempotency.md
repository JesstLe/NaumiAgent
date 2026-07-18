# HAR-10.5b Background Caller Idempotency

## 目标

HAR-10.5a 已能证明 Pursuit 行动进入了 `dispatched`，但 BackgroundRunner 不认识该行动身份。进程如果在
“Pursuit 已记 dispatched、后台 task ID 尚未回写”之间退出，新进程只能拒绝重跑，无法判断后台运行器是否
已经接纳任务。HAR-10.5b 把 Pursuit `dispatch_token` 接入 `background_run.idempotency_key`，让同一运行时及
正常重启后的重复接纳返回同一个 `bg_*` 任务。

这也是 ARC-04.2 immutable Tool job/idempotency key 的最小本地前置，不把当前 JSON BackgroundRunner 宣称为
daemon job service。

## 合同

`background_run` 新增可选 `idempotency_key`：

- 仅允许 1-128 位字母、数字、点、下划线、冒号和连字符；
- key 与 canonical resolved cwd、command、timeout 绑定；
- 相同 key + 相同输入返回已有 BackgroundTask；
- 相同 key + 不同输入明确拒绝；
- 一旦任务带 key，后续保存不能修改 key、command、cwd 或 timeout；
- 旧调用不传 key 时保持原行为，旧 `tasks.json` 缺少新字段时按空 key/1800 秒读取。

Pursuit 计算 action identity 时仍只使用业务参数。得到稳定 action key 后，再把它作为后台
`idempotency_key` 注入，避免 key 自己参与 digest 形成循环身份。

## Pre-spawn reservation

带 key 的任务在创建子进程前先持久化为 `preparing`：

```text
reserve(key, immutable input) -> preparing/bg_id
spawn process                -> running + pid
watch process                -> completed | failed | cancelled | timed_out
```

如果 spawn 明确失败，reservation 转为 `failed`，同 key 重试返回该失败回执，不再次 spawn。两个 Runner
实例在同一 Python runtime 内并发接纳相同 key 时，通过同路径共享 `RLock` 与 store reserve 只生成一个
task；每个 Runner 自身还用 async dispatch lock 串行化 lookup/reserve/spawn 边界。

Pursuit 遇到已持久化的 `dispatched` background action 时，不再直接报错：它携带原 key 重试
`background_run`。Runner 已接纳时返回原 task；尚未接纳时建立唯一 reservation。返回 task ID 后，Pursuit
继续把同一 action 写为 `waiting`，后续 status/output 回收仍走 HAR-10.5a 的 terminal ledger。

## Retention 与恢复语义

- 幂等回执在默认 7 天 retention 窗口内不受普通 `max_records=100` 数量裁剪，避免高并发完成后立即失去
  去重凭据；超过显式 retention 后允许与 artifact 一同删除。
- replay 先查询 key 再验证当前 cwd 是否存在。因此 worktree 已清理时仍能读取旧回执，而不会为了“验证路径”
  重新执行。
- `preparing` 表示接纳已持久化但进程启动尚未确认。它不会被同 key 自动重跑；现阶段需要 cleanup/reconcile
  判断陈旧 reservation，不能把未知副作用伪装成未执行。

## 验收证据

- 两个 Runner 并发使用同 key，只产生一个 task 和一个受管进程；
- Store/Runner 重开后同 key 返回已完成 task，不创建新进程；
- 原 cwd 删除后仍能读取已有回执；
- 同 key 异 command/cwd/timeout 拒绝；非法/超长 key 拒绝；
- spawn 失败保留 failed reservation，重复调用不再次调用 process factory；
- idempotent task identity 后续不可修改；
- legacy JSON 继续读取；未过期幂等回执不受 count prune；
- `tasks.json` 损坏时写路径 fail closed，原始字节不被空记录覆盖；
- Pursuit 从 `dispatched` 重试时实际把 action key 传给 background tool，并转入同一 waiting action；
- background/Pursuit/checkpoint/docs 只运行相关小模块测试，不运行全量测试。

## 当前不足与下一切片

本切片提供的是单 runtime + 正常重启持久去重，不是跨进程 exactly-once：

- `tasks.json` 没有 OS 级跨进程事务锁。两个独立 runtime 同时写同目录仍需 ARC-04 daemon 或 SQLite admission；
- runtime 在 reserve 后、PID 回写前硬崩溃时，`preparing` 只能标记未知，不能证明进程绝对没有启动；
- idempotency receipt 有明确的默认 7 天生命周期，不是永久 tombstone；
- HAR-10.5c 已根据 action ledger + BackgroundTask 状态自动解除证据充分的 `reconcile_required`；
- browser、subagent、外部 API 仍没有各自的 idempotent job contract。

HAR-10.5c 已实现类型化 background reconcile decision：机械区分 waiting、terminal、stale preparing、
dispatched-without-task 和 legacy unknown，只在证据充分时推进 checkpoint；未知状态继续 fail closed。设计见
[HAR-10.5c](HAR-10-5c-background-reconcile.md)。
