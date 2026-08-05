# ARC-04.5e3a / HAR-10.7h4 独立 Agent Model-only 执行内核

## 1. 目标与跨文档依赖裁决

ARC-04.5e2 已建立独立 Worker 的 exact Job owner lease、物理 slot 和加密 staging；ARC-04.6a 又建立
crash-before-start Supervisor fencing。继续线性扩张完整 Supervisor 不会让 Agent 真正在独立进程调用模型。

本切片依据以下文档共同选择最小执行纵向链：

- `13-cli-tui-claude-code-roadmap` 已有 Agent Control/Doctor 共享投影，不需要再造前端状态源；
- `14-future-architecture-refactor-plan` Phase E 要求高风险、长寿命执行从 Runtime 拆出，并具有 health、
  cancel/failure boundary 与独立测试；
- Harness 要求每个 running/terminal 状态有 durable authority 和 recovery 证据；
- 自进化闭环要求模型执行结果可审计、可隔离、可恢复，不能只靠自然语言回执。

因此本轮交付真实独立进程的 **无工具模型执行**：

```text
admitted AgentJob
  -> exact Worker slot + pre-start claim
  -> encrypted request/context staging
  -> encrypted model profile prepare
  -> child validates profile and returns prepared
  -> parent durably marks AgentJob running
  -> child performs one real Provider call
  -> encrypted terminal result
  -> AgentJob result + terminal payload + publication outbox atomic commit
  -> child receives exact commit fence and clears plaintext
  -> physical slot release
```

有任何 `tool_scope` 的 Job 都在 `mark_running` 和 Provider 调用之前拒绝。本切片不是完整独立 Agent tool loop，
不通过父进程偷偷代理工具来冒充 daemon 化。

## 2. 能力合同

新增 `agent_model_execution` Worker capability，并机械要求同时具备：

- `agent_control_transport`：认证本机 IPC；
- `agent_job_owner_lease`：exact durable Job owner/epoch；
- `agent_context_scope`：允许子进程持有经过认证的 request/context。

生产 Composition Root 将当前 `ModelConfig` 编码为私有内存载荷交给 Worker Factory；普通诊断对象不保存可
直接展开的公开 config 属性。Worker 只有配置了该载荷才声明 model capability 和 `accepting_jobs=true`。
Doctor/New UI/Textual TUI 共享显示“model-only 执行就绪、工具 RPC 未开放”，不把它称为完整 Agent Worker。

## 3. 密钥与模型配置边界

模型 profile 可能包含 API key，必须遵守：

1. 不作为进程命令行参数、环境诊断、日志或 UI 字段；
2. Factory 仅保留私有 bounded JSON bytes，不保留公开可 `repr` 的 ModelConfig；
3. profile 只在认证 child 已绑定 exact Job 后，使用进程级一次性 AES-256-GCM dispatch key 发送；
4. AAD 绑定 Worker incarnation、Job owner/claim、request digest、execution ID 与 profile digest；
5. child 在返回 `model_prepared` 前完成 envelope、字段集合、digest 与 ModelConfig 校验；
6. catalog path 必须为绝对路径，避免 child cwd 漂移加载其他配置；
7. Provider/解析异常只返回稳定中文错误与 reason code，不把异常文本、header 或 key 送回父进程。

profile 与 terminal JSON 都要求精确字段集合和硬大小上限；未知字段、尾随内容、摘要漂移、GCM tag 错误
全部 fail closed。

## 4. 两阶段 start fence

模型调用不能与 durable `mark_running` 竞态：

1. 父进程发送 `prepare_model`，child 解密并验证 profile、request、payload 和空 tool scope；
2. child 返回 exact `model_prepared`，此时未调用 Provider；
3. 父进程调用 `AgentJobStore.mark_running()`，取得当前 HMAC lifecycle receipt；
4. 父进程发送绑定该 running receipt digest 的 `start_model`；
5. child 只有精确消息匹配后才启动 Provider 调用。

因此 prepare 失败仍是安全 pre-start，可清除 child payload 并返回 admitted；start 后任何不确定故障都保持
running/recovery-required，绝不自动重放。

## 5. 模型执行约束

child 使用现有 `ModelRouter` 和同一 tier 解析规则执行一次真实非流式调用：

- system message 来自已被 request digest 绑定的 payload context；
- user message 来自已绑定 task；
- `tools=None`，Provider 若仍返回 tool call 则拒绝执行并产生安全 error terminal；
- request timeout 由 `asyncio.timeout` 强制；
- ModelConfig `max_tokens` 继续限制输出；
- 0 预算在 Provider 前拒绝；非零预算在真实 usage 返回后复验，超额响应不发布；
- response、error、model identity、token 与成本都有类型/范围/UTF-8 大小校验；
- 所有 Provider 派生字段先归一化为 bounded evidence，畸形 content/tool/usage/model/evidence 不进入 durable terminal；
- Provider response ID 只保留 SHA-256，不保留原 ID。

模型调用在线程内运行，child 主控制线程继续发送 heartbeat。父任务取消或 IPC 断裂时，父进程终止整个
child OS process；若 Job 已 running，则留给现有 recovery catalog/人工 unknown 裁决。

## 6. 终态提交与 publication

child 将 raw response/error 放入 AES-256-GCM terminal envelope，AAD 额外绑定 running receipt digest。
父进程解密后：

1. 重新签发低敏 `AgentWorkerResult`；
2. 调用既有 `AgentJobStore.finish()`；
3. 同一 SQLite 事务写入 terminal state、加密 terminal payload 和 pending publication outbox；
4. 把 exact result digest 与 terminal lifecycle receipt digest 回给 child；
5. child 只有验证 commit 消息后才清除 bound request/context/profile/result；
6. 父进程最后释放物理 slot。

若父进程在 terminal commit 后、child ack 前崩溃，durable Job/outbox 已是权威，publication recovery 可继续；
若在 commit 前崩溃，Job 保持 running unknown，不发布未认证内容。

## 7. 聚焦验收

- 真实 spawned child 访问本机 OpenAI-compatible HTTP Provider，不使用模型 mock；
- request 中的 context/task 与真实 HTTP body 一致；
- 加密 profile 中的 API key 确实成为真实 HTTP Authorization，且不进入 Registry/Harness/AgentJob 持久化；
- child heartbeat 在模型调用期间继续推进；
- Job 单调经过 admitted/claimed/running/completed；
- terminal response 可从 AgentJob key 重新认证恢复，outbox 同事务出现 pending；
- Worker physical slot 在 commit 后释放；
- Worker Registry/Harness/AgentJob DB 不出现 API key 或明文 model response；
- Provider response ID 只以 SHA-256 返回；
- tool scope 在 running/Provider 前拒绝，并能安全 release 到 admitted；
- child 在 running 后丢失时 Job 保持 running，slot 收口，不自动重放；
- model profile/result 未知字段、0 budget 和 capability 缺失 fail closed；
- model-capable Worker 在 pre-start 崩溃时仍可由 ARC-04.6a 合法 fencing/requeue。

只运行 Agent Worker model/process、Worker contract/Supervisor、AgentJob、Runtime Composition、authority health、
Doctor 和文档治理小模块测试，不运行全量测试。

## 8. 自我审视与未完成

本切片是真实独立模型执行，但仍不是用户默认的完整独立子 Agent：

- 尚未把 SubAgentManager 生产调度切到该 Worker；当前是 Composition-owned 可调用内核；
- tool scope 非空全部拒绝，尚无加密 Tool RPC、父 Runtime permission bubbling 或 tool result envelope；
- 只有一次非流式模型 call，尚无多轮 tool loop、stream delta 或 thinking 实时投影；
- Provider response ID digest 目前随进程 outcome 返回，尚未进入 durable AgentJob result schema；
- 非零成本预算只能在 Provider usage 返回后复验，不能宣称绝对不超额；后续需基于模型 rate-card、输入
  token 预估和 output token cap 做调用前 hard reservation；
- 取消通过终止 Worker 收口，running Job 进入 recovery-required；尚无 Provider cancellation API；
- 没有自动 scheduler、跨 workspace/provider fairness、长期 Worker 池、upgrade drain 或跨主机 topology；
- 真实进程/HTTP 验收来自当前 Darwin 主机，Linux/Windows 仍需平台矩阵。

下一切片 `ARC-04.5e3b / HAR-10.7h5` 应实现加密 Tool RPC 与 parent-side exact permission/tool authority，随后
才能把生产 SubAgentManager 的具备工具任务切到独立 Worker。不得在工具权限链完成前默认替换 embedded
执行，也不应回头一次做完整 ARC-04.6。
