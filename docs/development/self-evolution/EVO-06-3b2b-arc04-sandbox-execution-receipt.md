# EVO-06.3b2b ARC-04 Sandbox Execution and Receipt

## 目标

消费 EVO-06.3b2a 的 current content-addressed Request，在 ARC-04 一次性 Shell Worker 中逐场景执行
sealed Capability，并把 oracle、权限观察、Worker lifecycle、Run Grant 和 source identity 收口为不可变
Execution Receipt。

本切片回答“候选在给定场景中是否真的按规格运行”，但仍不回答“是否应注册或上线”。即使全部通过，
`registry_authorized`、`shadow_authorized` 和 `executable` 也永久为 false；短期 Registry lease 是后续独立
authority 切片。

## Authority chain

执行入口只接受 `evolution_capability_sandbox_execute` 当前工具调用生成的父权限回执。Service 机械验证：

- parent receipt 允许执行、绑定非空 run ID；
- tool name 与 `{candidate_id, run_id}` arguments digest 精确匹配，receipt run ID 必须等于参数；
- delegated scope 精确包含 `bash_run`；
- Request、Binding、Artifact、Specification 和 Git source 仍 current；
- Request 未声明当前无可信 adapter 的 `network`、`browser` 或 `secrets` 权限；
- Request 的 Python executable、实现、平台和 driver policy 形成 runtime identity digest。

随后先按 Request 取得持久、带 epoch、可过期接管的 execution claim，再取得 fenced
`HarnessRunKind.RUNTIME` lease并签发短期 `RunDelegationGrant(bash_run)`。每个场景由
`HarnessSandboxEvalExecutionKernel` 组合 UI-12 child receipt、ExecutionGrant、ToolJob、ephemeral Worker
registration/capacity reservation 和 Tool lease。任何 identity、epoch、lease、grant 或 parent receipt 漂移均
在 payload 发送前 fail closed。

## Exact source 与 timeout

Runner 从 Request 的 exact Git commit/tree 读取 blob，并只叠加 Request digest 绑定的 candidate、driver、
permission manifest 和 scenario input。宿主工作树内容不会复制进执行快照，snapshot 在终态删除。

Driver 以 `asyncio.wait_for(timeout_ms)` 执行 `Tool.execute()`，这是场景语义 timeout。ARC-04 Shell envelope
额外增加固定 15 秒启动/清理余量，避免 100ms..300s 的场景预算被进程注册、sandbox 启动和终态持久化
偷走；该余量不扩大候选代码的执行时间。

## Oracle 与权限判定

Worker stdout 必须是单一 JSON envelope，额外日志、非 JSON、未知字段或缺失 permission observation 都
视为 infrastructure failure，不能被模型解释。

- `result`：将返回值规范化为 Scenario Binding 的 result expectation shape 后比较 SHA-256；
- `error`：只比较 ARC-01.3d1 声明式 error code，`retryable` 保留在运行 envelope 但不改变 oracle；
- timeout、resource limit、cancelled、source stale 和 infrastructure failure 分别记账；
- Python audit hook 在实际操作前阻断 workspace scope 外读写、未声明 process 和所有 network event；
- OS Shell Worker 同时强制 network disabled、资源上限、非 PTY 与一次性 snapshot；audit hook 不替代 OS 隔离；
- observation 有 128 allow + 128 deny 的有界集合，溢出即阻断，Receipt 只保存规范 scope/decision 和摘要。

## 不可变 Receipt 与动态撤权

`EvolutionCapabilitySandboxExecutionReceipt` 绑定：

- Request/Candidate/Binding、Git revision/tree 和 overlay digest；
- parent permission receipt 与 Run Grant digest；
- runtime identity；
- 每个场景的 expectation/actual/permission digests、状态、job ID、lifecycle receipt、snapshot manifest 和耗时；
- 全场景执行/通过、权限观察完整、exact revision、ARC-04 Worker、Run Grant revoked、Runtime lease released；
- Registry/Shadow/executable 全部 false。

Receipt 只在 Run Grant 撤销和 Runtime lease 释放均成功后落库；Receipt 与 execution claim 的 terminal
状态在同一个 SQLite `BEGIN IMMEDIATE` 事务中提交。活跃 claim 阻止其他进程重复执行，只有超时 claim
可以按递增 epoch 接管；旧 epoch 永远不能提交结果。Store 对每个 Request first-terminal-wins，重复执行返回
同一 Receipt；payload 与持久摘要每次读取重验。`inspect` 每次重验 Request，来源漂移后历史
Receipt 保留但 View 变为 `revoked`。当前 v1 不自动重试 terminal failure，避免重复候选副作用；后续若增加
retry，必须使用新的显式 retry authority 和 attempt identity。

## 双通道与 UI

- Slash：`/evolution capability-run <candidate-id>`；
- Agent Tool：`evolution_capability_sandbox_execute(candidate_id=..., run_id=...)`；
- New UI：typed `evolution/review/request` 的 `capability-run` action；
- CLI/TUI/New UI 共用 Tool、PermissionChecker、Service 和 renderer；
- UI 显示 Receipt ID、状态、场景通过数、source currentness、权限观察完整性和逐场景状态，不显示
  candidate source、scenario arguments、oracle value 或 stdout。

## 验收证据

- [x] 真实临时 Git 仓库通过系统可用的 ARC-04 sandbox backend 执行，不是 mock Worker；
- [x] exact revision + overlays 被物化，候选在 snapshot 中读取允许的真实 fixture；
- [x] result oracle 和 permission observation 通过并形成 content-addressed Receipt；
- [x] parent/child permission、Run Grant、Worker lifecycle、capacity 和 lease 链完整；
- [x] Run Grant/Runtime lease 清理后才持久化 Receipt，snapshot 无残留；
- [x] 重复执行幂等，Receipt payload 篡改 fail closed；
- [x] 两个独立 Store 实例不能并发 claim 同一 Request，过期接管递增 epoch；
- [x] driver setup failure 在观察器尚未启动时仍形成不伪造 observation 的 infrastructure Receipt；
- [x] Request driver 已覆盖声明式 error 与越权读取阻断；
- [x] Slash、Agent Tool、New UI typed action 与 fallback 使用同一执行入口；
- [x] 仅运行相关 Ruff、py_compile、Python/Node 小模块测试和文档治理，不运行全量测试。

## 自我审视与下一步

本切片已有“Request → 真实隔离执行 → Receipt”的闭环，但通过 Receipt 仍不能进入 ToolRegistry。下一最小
切片 EVO-06.3b2c 应签发短期、可撤销、namespace 隔离的 Registry lease：只能消费 current、passed、
permission-complete 的 3b2b Receipt；不得覆盖内置 Tool；lease 到期、Request/Receipt 漂移或 Runtime 重启时
自动卸载。随后再进入 EVO-06.4 Shadow evaluation，不能直接跳到 Limited Activation。

当前限制：network/browser/secrets capability 继续 fail closed；permission observation 只覆盖 Python audit
事件与 ARC-04 Shell sandbox 能表达的边界；Windows backend 的同等 OS 隔离强度仍需在平台 lane 独立验证，
不能以 macOS 本地 E2E 代替跨平台声明。
