# HAR-09.6c2a3e Remote Execution Authorization

## 状态

已实现。

## 目标

在 `HAR-09.6c2a3d` 的 current Delivery receipt 与真实远端 Eval 启动之间建立不可绕过的执行边界。只有 exact
Worker Identity 对一次性 Start challenge 签名，并且父权限、Claim、Delivery、Worker、Runtime lease 与 Run Grant
在提交时仍同时有效，才形成短期 `installation_authorized=true`、`execution_authorized=true` view。

Delivery ACK、公开 Worker ID、queued Dispatch、Claim receipt 或生成 challenge 均不能直接启动 Eval。本切片不接收
result，不声称进程已经启动，不写 H5a，不改变 Behavioral Matrix，也不授予 learning/promotion authority。

## 两阶段协议

```text
current Delivery receipt + current exact Claim receipt
  + current Worker Identity/incarnation
  + exact Eval Suite/resource budget
  + durable parent Permission receipt (delegates bash_run)
  -> bounded one-time Start challenge
  -> Worker Ed25519 signs canonical Start payload
  -> verify signature before any execution resource side effect
  -> acquire exact Runtime lease
  -> issue non-transitive bash_run-only Run Grant
  -> re-read clock and all current authorities
  -> atomically close challenge and persist execution authorization
```

Prepare 只写 session SQLite challenge，不获取 Runtime lease、不签发 Run Grant。伪造 signature、过期 challenge 或漂移的
Delivery/Claim 在执行资源分配前失败。Run Grant 或 lease 已创建后若 authority 重验、deadline 或 authorization Store 写入
失败，Service 会补偿撤销 Grant 并释放 exact lease epoch。

## Start payload

Worker 签名的 canonical payload 不依赖模型文案，固定绑定：

- stable Attempt ID、Dispatch attempt number、Start nonce、issued time 与 start deadline；
- exact Delivery offer/receipt、archive/manifest、target baseline 与 release target；
- exact Dispatch、latest Claim receipt/lease epoch、Worker Identity/incarnation/Contract；
- 完整 `ReleaseRuntimeEvalRequest`、suite id/digest、repetitions 与 case/total time budget；
- Worker Contract-bound memory ceiling，以及由 Eval 机械投影的 CPU/wall/output limits；
- ephemeral workspace、network default deny、environment allowlist、resource enforcement、process-tree cancel 与 artifact digest；
- durable parent Permission receipt id/digest，且唯一委托工具为 `bash_run`；
- `installation_authority=false`、`execution_authority=false`、`result_authority=false`。

Attempt ID 由 Delivery receipt digest、Dispatch digest/attempt 与 suite digest 派生。Challenge ID 额外绑定随机 32-byte nonce；
同一 current attempt 的跨 Service 并发 prepare 由 SQLite 选择首个 pending challenge 并收敛，不能并行形成两个 Start token。

## 资源与时间边界

- challenge TTL 为 1..120 秒；deadline 取请求 TTL、Delivery expiry、Claim lease、reservation 与父权限 300 秒新鲜度的最早值；
- wall/CPU budget 为 `ceil(total_execution_budget_ms / 1000)`，单次最大 3600 秒；
- 单 case output 固定为 Runtime Eval 512 KiB 上限；aggregate output 为该上限乘 repetitions；
- Worker Contract 必须真实覆盖 memory/CPU/wall/output，并声明完整隔离能力；
- submit 前必须存在足以覆盖完整 Eval budget 的 Claim/Delivery/reservation 窗口，不截断 suite 后冒充完整授权；
- authorization expiry 机械取 Eval budget、Claim、Delivery、reservation、Run Grant 与 Runtime lease 的最早值；
- 外部 lease/Grant 写入后重新读取 runtime clock；跨过 start deadline 时补偿清理，不能使用旧开始时间提交。

## 父权限与 bypass

Tool metadata 声明 `delegated_tool_names=("bash_run",)` 与 persistent authorization。Runtime 因此先为本次 ToolCall 保存
durable Permission receipt，并在 task-local capability 有效期内交给 prepare；challenge 保存该 exact id/digest。父回执必须：

- 允许执行并绑定非空 run id；
- 来源为 policy、bypass 或一次用户确认，不能是 delegated child；
- 明确允许委托 `bash_run`；
- 在 Run Grant 签发时不超过 300 秒。

所有 Permission Mode 均不显示本工具的二次确认；bypass 直接通过权限政策，但不能跳过 Worker signature、Delivery/Claim
fencing、lease、Run Grant、时间窗口或 durable Store。

## SQLite 与跨 Store Saga

Start/Authorization Store 与 Delivery、Claim、Dispatch、Identity 共用 session SQLite。challenge 写入和 authorization
写入均使用 `BEGIN IMMEDIATE`，事务内重读：

- delivered offer 与 exact Delivery receipt；
- latest Claim receipt；
- exact Dispatch、Identity 与 suite/budget；
- offer/Claim/reservation 的当前时间边界。

Worker signature verification、authorization insert 和 `pending -> authorized` challenge close 在同一事务完成。相同 signature
重放幂等返回同一 authorization，不同 signature 不能关闭已授权 attempt。

Permission Store、Run Grant Store 与 Harness Store 是独立权威库，因此不伪称跨库 ACID。Service 采用显式 Saga：先验证、后
lease、再 Grant、重验 authority、最后 session SQLite commit；末段失败时撤销 Grant 并释放 fenced lease。

## 动态 authority

`inspect` 每次重新验证：

- Delivery receipt/offer 与 exact latest Claim；
- Dispatch、Worker active incarnation、Identity 与 Contract；
- parent Permission receipt digest/source/scope；
- Run Grant state、内容与 exact Runtime lease owner/epoch/expiry。

Claim renewal、higher Worker epoch、Delivery/Dispatch/Catalog/Health/capacity 漂移、Grant revoke、lease release/takeover 或
artifact 损坏均使 view 进入 `stale/expired`，并关闭 installation/execution authority。历史 authorization 仍可审计，但不是
current capability。

## 权威边界

Current authorization 只表示 exact Worker 可在受限环境中安装已交付 artifact 并执行该 Eval request。持久 artifact 仍固定：

- `execution_started=false`：Worker 签署的是启动前承诺，尚无进程/结果证据；
- `result_authority=false`、`result_received=false`；
- `learning_authority=false`、`promotion_authority=false`。

自动远端 daemon push、Worker 侧 lease/Grant enforcement RPC、真实进程 start receipt 和取消传播仍是后续切片；
签名结果摄入已由 HAR-09.6c2a3f 独立完成。当前协议对象通过共享 Tool/Slash 传递，不宣称生产级远端执行集群已完成。

## 双通道入口

- Agent Tool：`evolution_post_rollback_remote_execution`；
- CLI/TUI/New UI 共享 Slash：
  - `/evolution outcome-authorize-behavior prepare <delivery-id>`；
  - `/evolution outcome-authorize-behavior submit <start-challenge-id> <worker-signature-base64>`；
  - `/evolution outcome-authorize-behavior inspect <attempt|challenge|authorization-id>`。

Prepare 回执包含完整 canonical challenge JSON 与 signable SHA-256；submit/inspect 显示 Attempt、Worker signature、Run Grant、
lease epoch、expiry 和当前 authority，不把 Tool 文案作为状态真相。

## 验收证据

`tests/unit/test_post_rollback_remote_execution_authorizations.py` 使用真实 Delivery/Claim/Identity、Ed25519、Permission Store、
Run Grant Store、Harness lease 与 SQLite 验证：

1. prepare 绑定 exact Delivery、Eval Suite、budget、attempt 与 deadline，且不创建 lease/Grant；
2. exact Worker signature 形成 current authorization，相同签名重放幂等；
3. forged signature 在任何 execution resource side effect 前失败；
4. 两个独立 Service 并发 prepare/submit 收敛到一个 challenge 与 authorization；
5. 缺失或超过 300 秒的父 Permission receipt 不能形成 challenge；
6. Grant revoke 与 Claim renewal 动态 fencing current authority；
7. 外部 Grant 写入跨过 start deadline 时撤销 Grant、释放 lease且不写 authorization；
8. authorization Store 故障执行相同补偿；
9. Tool、Slash、moderate/bypass 共享同一 Service、持久父权限且无二次确认；
10. session authority Store split 在构造期失败，challenge SQLite 篡改读取时失败关闭；
11. 默认 AgentEngine 构造不获取 lease、不签发 Grant、不读取 Worker private key。

## 后续状态

`HAR-09.6c2a3f` 已实现 signed result ingestion：Worker 在本 authorization/Attempt 下提交 bounded typed Runtime Eval
manifest、进程平台身份与逐 repetition 证据，Control Plane 在 current window 原子准入并写入既有 H5a/H5c 权威。
已准入结果支持授权过期后的幂等恢复；无 durable admission 的迟到自报失败关闭。`HAR-09.6c2b1` 已进一步聚合完整
Behavioral Matrix Core、6c2b2 typed 双端详情、6d1 长期观察契约与 6d2 managed runtime admission 均已完成；
下一步为 6d3 长期窗口评估。
