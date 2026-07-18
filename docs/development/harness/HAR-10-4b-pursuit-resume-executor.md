# HAR-10.4b Pursuit Resume Executor

## 目标

让 `/pursue resume <run_id>` 不再只检查持久状态，而是在获得新的 HAR-10.1b lease epoch 后，从经过完整性
校验的 checkpoint 重建 Pursuit planner 状态并继续执行。同时必须区分安全恢复与可能已经产生外部副作用的
in-flight action，禁止通过盲目重放制造重复修改、重复命令或重复外部请求。

## 恢复输入

resume executor 只接受 HAR-10.4a `PursuitCheckpoint`。恢复时机械重建：

- GoalSpec、成功标准状态、验证证据和约束；
- 最近 20 轮 IterationCheckpoint，用于上下文和停滞判断；
- 累计 token、成本、活动耗时以及 iteration/cost/time 三类预算；
- checkpoint sequence、后台等待项、待办和 pending interaction；
- worktree 名称与绝对路径。

Goal parser 不会再次调用，已经消耗的 token、成本、轮次和活动时间不会归零。报告中的总轮次使用持久轮次，
不会因为 history 有界而错误缩小。

## 一致性门

进入 planner 前必须满足：

1. checkpoint 摘要、schema、ID、run ID 和 sequence 已由 Store 验证；
2. checkpoint iteration 与 PursuitRun 摘要一致；
3. evidence cursor 不超过当前持久证据数量；
4. checkpoint 与 PursuitRun 的 worktree 完全一致，且非空 worktree 仍真实存在；
5. 不处于 `checkpoint_error`；
6. 没有尚待用户回答的 pending interaction；
7. 不处于 `action_inflight` 或含义不明确的旧版 `execute + pending_actions`。

任何不一致都不会调用模型或工具。交互等待进入 `interaction_required`；可能有副作用的行动进入
`reconcile_required`，下一步明确指向 HAR-10.5。

## 安全阶段协议

新 planner 行动的 durable phase 顺序固定为：

1. `planned`：计划已持久化，尚未发送给工具；
2. `action_inflight`：即将发送或正在执行，checkpoint 已先于 tool dispatch 落盘；
3. `action_result`：结果已通过当前 epoch fencing，证据和空 pending actions 已持久化；
4. `waiting`、`verify` 或下一轮 `assess`。

进程在 `planned` 崩溃可从 assessment 重新规划；在 `action_inflight` 崩溃必须 reconcile，不能根据模型猜测
是否成功。该协议同时覆盖普通 plan 和 stagnation recovery action。

## Lease 与继续执行

resume 与新运行共用同一个 `PursuitLeaseSession`：

- live owner 存在时保持拒绝且不改写 PursuitStore；
- released/expired lease 由新 owner 获取，epoch 单调增加；
- state restore、assessment、plan、action、result、terminal 都继续使用 fencing boundary；
- continuation 完成、waiting、blocked 或预算终止后释放精确 epoch；
- 用户回执显示输入 checkpoint ID 和本次 lease epoch。

`PursuitResumeTool` 在 checkpoint 已恢复且新 lease admission 成功后立即返回后台回执，不会让当前对话等待
小时级循环。若 admission 前已经发现 live owner、状态损坏、interaction 或 reconcile blocker，则直接返回
具体原因；10 秒内无法确认准入会取消任务，不留下不可见的幽灵 continuation。

## 后台与交互

已有后台任务先读取真实 status/output。仍在运行时只推进 reconciled checkpoint sequence 并保持 waiting，
不消耗模型轮次；全部完成后才恢复 GoalSpec 并进入新 assessment。pending interaction 在 HAR-10.6 提供答案
写回协议前保持暂停，同样不消耗模型轮次。

后台输出和其他 `PursuitRun.evidence` 会以最近 10 条、有界摘要进入 assessment 的状态证据，避免只把结果写进
SQLite 却不给恢复后的 planner 使用。

## 预算语义

checkpoint 保存的是累计活动执行时间。进程关闭期间不消耗 active runtime budget；恢复后从原累计值继续。
若 iteration、cost 或 active time 在恢复时已经达到上限，循环会在新 assessment 之前进入
`budget_exceeded`，不会额外调用模型。

## 验收证据

- 安全 checkpoint 恢复后不调用 Goal parser，从下一轮 assessment 继续；
- criteria、history、预算、token/cost、sequence 和 operational config 保持；
- 达到累计预算时，新 assessment 调用次数为零；
- `action_inflight` 和旧版歧义 action 均停在 `reconcile_required`，工具调用次数为零；
- pending interaction 停在 `interaction_required`，模型调用次数为零；
- action dispatch 前真实保存 `planned → action_inflight`，结果后保存 `action_result`；
- 完整 leased run 首次执行使用 epoch 1，resume continuation 使用 epoch 2 并可靠释放；
- 工具层在 durable admission 后返回后台回执，准入前 blocker 返回原始原因；
- checkpoint、Pursuit、lease 小模块测试通过。

## 当前不足与下一切片

HAR-10.4b 不尝试判断 in-flight shell、browser、background 或外部 API 是否已经成功，也没有 durable action
idempotency key。该责任属于 HAR-10.5 Resume/Reconcile。HAR-10.6a 已实现 durable interaction authority、
超时和 takeover fencing；HAR-10.6b 已接入类型化问题展示、用户答案写回、checkpoint stable ID 引用和
pending/answered/expired 恢复核对。UI-18.4b 已补齐 TUI parity；显式 cancel 仍未完成。
最终报告本身会新增一次模型调用；该调用发生在 terminal checkpoint 之后，因此报告 token
尚未计入 terminal checkpoint，需在后续 completion receipt 计量收口中修正。
