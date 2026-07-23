# ARC-04.5c Embedded Agent Durable Dispatch

## 1. 目标与交付边界

ARC-04.5a 已提供不可变 Agent request/result contract，ARC-04.5b1/5b1a 已提供显式 Runtime key
与 authenticated encryption，ARC-04.5b2 已提供 durable Agent Job lifecycle authority。本切片把
现有生产 `SubAgentManager` 接入同一权威链：

```text
assemble exact context
  -> issue request contract
  -> durable admit
  -> claim + recover exact encrypted payload
  -> running fence
  -> model execution + claim renewal
  -> issue result contract
  -> durable terminal commit
  -> publish result / message / UI evidence
```

本切片仍是 embedded Runtime 执行，不是独立 Agent daemon，也不声称已经具备跨主机 scheduler、
capacity fairness、模型响应恢复或 Supervisor takeover。

## 2. 模型调用前的持久派发屏障

`SubAgentManager` 在 blackboard、待处理消息和调用方上下文完成最终组装后，先签发
`AgentWorkerRequest`。注册进程内 active execution 后必须依次完成：

1. 用精确 request 和 raw payload 执行 `AgentJobStore.admit()`；
2. 由当前 manager instance 的 opaque owner ID 执行 `claim()`；
3. 使用 exact claim epoch 解密 `recover_payload()`；
4. 逐字段比较恢复 payload 与即将执行的 task/session/context/topic；
5. 用相同 owner/epoch 执行 `mark_running()`。

只有五步全部成功，才允许发送 `subagent started`、执行 hooks 和创建 `agent.execute()` task。
因此 key 缺失、request identity 冲突、密文认证失败、owner/epoch 漂移或 running transition 失败时，
模型都不会被调用。

缺少 Runtime key 的用户回执明确给出：

- 交互安装：`naumi runtime-key init`；
- 自动化环境：`NAUMI_RUNTIME_PAYLOAD_KEY`。

普通启动、Doctor、Agent Control 和 Store Catalog 仍不会隐式创建 key 或访问钥匙串。

## 3. Claim lease 与运行中 fencing

embedded owner 使用 90 秒 claim lease，并每 30 秒续期。renewal 与模型 task 是两个独立协程：

- 续期成功会更新持久 lifecycle receipt，并保持 Agent Control 中的 job state；
- 续期失败会写入稳定 `agent_job_claim_renewal_failed`，将 execution 置为 stopping；
- 如果模型 task 已存在，立即取消；
- 如果失败发生在 hook/startup 阶段，preflight 会在创建模型 task 前拒绝执行；
- preflight 与 task attach 之间再次观察到失败时，attach 会立即取消 task。

续期失败不会伪装成用户主动停止；`stop_requested` 仍只表示用户/父执行请求。持久 job 的最终状态由
后续 terminal fence 决定；若 owner/epoch 已失效，结果进入隔离而不是伪造 terminal success。

## 4. 终态提交与发布屏障

模型返回、超时、最大轮数、用户停止或父任务取消后，manager 先停止 renewal，再签发
`AgentWorkerResult`。只有 job 当前处于 `running` 且 exact owner/epoch 仍有效，才调用
`AgentJobStore.finish()`。

终态提交允许一次立即幂等重试。两次均失败时：

- 不向调用方返回模型 response；
- 不向 message bus 发布 completion；
- execution 记录保留原 result digest，便于后续对账；
- 返回稳定中文错误“持久终态提交失败，结果已安全隔离”；
- Agent Control 显示 `agent_job_terminal_commit_failed`；
- Store 中的 running job 留给 expiry + recovery-unknown 流程收口。

第三方 Agent 返回负 token、无效费用或非法状态，导致 result contract 无法签发时，同样不得发布原结果，
并显示 `agent_worker_result_invalid` + `agent_job_terminal_receipt_invalid`。这比 ARC-04.5a 的
“仅显示合同降级但继续返回结果”更严格，是 durable publication barrier 建立后的预期语义升级。

## 5. 取消、异常与重启边界

- 用户 `stop_execution()`：取消模型 task，签发 `cancelled` result，并在 live fence 下提交 durable
  cancelled terminal；
- 父协程取消：先完成相同 terminal commit，再向父调用方继续传播 `CancelledError`；
- started event 或 hook 异常：若 job 已 running，必须先提交 error/cancelled terminal；
- start fence 失败：模型未调用，job 保持 claimed，等待 lease expiry；不能猜测持久状态；
- Runtime 在 running terminal commit 前崩溃：ARC-04.5b2 将其列为 recovery required，禁止自动重跑。

当前没有自动恢复 claimed job 的 scheduler，也没有恢复模型 response 原文。Job Store 证明“请求是什么、
谁拥有、执行到了哪个副作用边界、终态摘要是什么”，不等同于完整对话结果存储。

## 6. Agent Control / New UI / TUI 共享证据

Agent Control schema v2 继续使用 additive informational fields，execution descriptor 新增：

- `worker_job_id`
- `worker_job_state`
- `worker_claim_epoch`
- `worker_job_failure_code`

Python authority 对 job state 使用闭集校验，claim epoch 必须为非负整数。New UI protocol normalizer 与
Textual formatter 都只消费该 authority：

- New UI 详情显示短 job ID、state、epoch；失败码使用警示色；
- Textual TUI 显示同一四字段，不自行读取 SQLite；
- active、terminal、fenced 和 isolated 状态不会由前端推断；
- task/context/response、owner ID、claim expiry 和密钥不进入 UI payload。

## 7. 聚焦验收证据

- 正常委派产生 admitted → claimed → running → completed receipt chain；
- 关闭后重新读取 Store，terminal result digest 与 Agent Control 一致；
- SQLite bytes 不包含 task 或模型 response 原文；
- 缺 key 时给出初始化入口，模型调用计数为 0；
- running fence 失败时保持 claimed，模型调用计数为 0；
- hook 阶段 claim renewal 失败时 preflight 阻止模型调用并提交 cancelled；
- 用户取消与父任务取消均产生 durable cancelled；
- terminal commit 连续失败时 response 为空、message bus 不发布、降级码稳定；
- invalid result contract 不生成虚假 result digest，也不发布未认证结果；
- Composition Root 注入的 Store 与 manager 使用同一实例；
- Python Agent Control、Bridge、New UI protocol/render 与 Textual formatter 显示同一 job 证据。

验收只运行上述小模块测试，不以全量测试代替切片证明。

## 8. 自我审视与未完成

本切片没有完成：

- 独立 Agent worker 进程、Worker Registry incarnation 与 Supervisor；
- 跨进程 capacity reservation、durable waiting fairness、priority/affinity；
- claimed job 的自动 scheduler、重启接管和显式人工恢复动作；
- response 原文的加密 terminal payload、父进程崩溃后的可展示结果恢复；
- Provider request ID 对账、unknown 人工裁决；
- key rotation/reencrypt、Agent Job retention/GC、备份恢复；
- Windows/macOS/Linux 独立打包后的 credential/SQLite/崩溃矩阵。

后续不应继续线性做完整 ARC-04。应重新比较 Harness、ARC-06 与 UI 依赖，优先选择能产生独立用户价值的
最小切片。后续依赖审计已选择并完成第一项：

1. `HAR-10.7c / ARC-06.2c Agent Capacity Admission`：已让跨 Runtime Agent 等待消费共享 durable
   capacity，见 [设计与验证](ARC-06-2c-durable-embedded-agent-capacity.md)；
2. `ARC-04.5d Recoverable Agent Result Publication`：解决 terminal commit 后、父进程发布前崩溃造成的
   可展示结果缺口；
3. Agent recovery UI：只读列出 claimed/running/unknown 并提供精确人工动作。

选择前必须检查三者与当前 Harness durable queue、Agent Control 和 Supervisor 文档的依赖，不默认沿
ARC 编号继续。
