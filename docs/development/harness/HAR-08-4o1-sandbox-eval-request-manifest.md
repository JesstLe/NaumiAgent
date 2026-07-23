# HAR-08.4o1 Sandbox Eval Request Manifest

## 状态

已实现，2026-07-23。

本切片是 HAR-08.4o“取消后显式 retry authority”的必要前置，只解决一个问题：
在进程退出、取消完成或 UI 重连后，服务端如何从 durable authority 恢复最初获准执行的
Sandbox Eval Request，而不是信任客户端重新提交 checks、samples、batch 或工作区。

本切片不开放 retry 命令，也不创建新 admission ticket。只有 HAR-08.4o2 完成 accepted cancel
receipt、新 action、新 execution authority 与真实重新执行的原子绑定后，UI 才能显示 retry。

## 已发现的缺口

HAR-08.4h 的 `HarnessSandboxEvalRequest` 已包含：

- canonical workspace；
- ordered Profile check digests；
- requested samples 与总预算；
- clean Git commit/tree identity；
- batch、suite、Profile digest；
- 完整 request identity 与 SHA-256。

但此前该对象只存在于发起执行的进程内。H5a 保存的是逐样本结果，admission ticket 保存的是容量
claim，cancel receipt 保存的是取消裁决；三者都不能无歧义重建原始 request。

因此，若直接实现 retry，只能错误地选择以下任一种：

1. 信任 UI 再次提交原始参数；
2. 只创建新 ticket，但没有可执行 request；
3. 重新从当前 Profile/Git 编译一个可能已经漂移的新请求。

本切片明确拒绝这三种路径。

## Store v19

新增 `harness_sandbox_eval_requests`：

| 字段 | 约束 |
|---|---|
| `workspace_root` | canonical absolute workspace，服务端产生 |
| `request_id` | `hseval_<24 hex>`，workspace 内主键 |
| `request_sha256` | request semantic authority，workspace 内唯一 |
| `batch_id` | workspace 内唯一，禁止同 batch 改写请求 |
| `suite_id` | ordered checks 与预算形成的稳定 suite identity |
| `request_json` | Pydantic 严格模型的完整不可变 JSON |
| `created_at` | 首次成功持久化时间，幂等重放不覆盖 |

请求 JSON 上限为 256 KiB。当前模型只包含路径、Git identity、check/spec/argv digest 和预算，
不保存命令参数明文或凭据。

## 写入与读取语义

`HarnessStore.record_sandbox_eval_request()`：

1. 再次执行严格 Pydantic 校验与 request digest 校验；
2. canonicalize workspace 和 timestamp；
3. `BEGIN IMMEDIATE`；
4. 同时按 request ID、digest、batch 查询；
5. 完全相同的并发或重启重放返回首次记录；
6. 任一 identity/digest/batch 被不同请求占用时冲突关闭；
7. 首次请求写入 immutable manifest。

`HarnessStore.get_sandbox_eval_request()` 只接受 canonical workspace 与精确 request SHA-256。
读取时重新校验 request JSON、摘要、workspace、request ID、batch 与 suite；任一持久字段被篡改均
作为 Store 损坏失败，不向上层返回部分对象。

## 原生执行顺序

`HarnessSandboxEvalExecutor.execute()` 的新顺序是：

1. 验证 request 属于当前 workspace；
2. 验证当前工具权限回执与原始 checks/samples/batch 精确匹配；
3. 复验当前受信 Profile；
4. 持久化 Request Manifest；
5. 进入 durable admission；
6. 获取 Runtime lease/Run Grant；
7. 执行并写入 H5a。

因此无权限或 Profile 已漂移的请求不会留下可 retry manifest；执行中断、进程退出或用户取消前，
合法请求已经能够由新进程恢复。

## 验收证据

- Harness Store schema 从 v18 升级到 v19，完整表目录精确校验；
- 真实临时 Git 仓库编译 request，并由新的 Store 实例从 SQLite 恢复；
- 相同请求以不同时间幂等重放仍保留首次事实；
- 两个独立 Store facade 并发写入收敛为一条记录；
- 相同 batch 的不同 Profile authority 被拒绝；
- 手工篡改持久 request JSON 后读取失败；
- 样本执行中断后，新的 Store 实例仍可恢复 request 与已有 H5a 前缀；
- 聚焦 Store/Request/Service 测试、ruff、Python compile 与 diff check 通过。

## 自我审视与限制

已确认：

- manifest 不是 prompt 套壳，而是有数据库 schema、原子事务、模型复验和篡改检测的真实 authority；
- 不信任客户端 workspace 或原始执行参数；
- 不允许同 batch 在重试时静默漂移到另一个 request；
- 并发与进程重启走真实 SQLite，而不是进程内缓存。

仍未实现：

- accepted cancel receipt 到 request manifest 的 retry transaction；
- retry 专属 action/receipt；
- 与原 request 分离的新 execution authority；
- 新 ticket、Run Grant、真实恢复执行；
- Slash、Bridge、New UI 与 TUI retry surface。

## 下一切片

HAR-08.4o2 应在一个 durable transaction 中验证 accepted cancel receipt 与其原 ticket/request
authority，消费该 receipt 创建新的 retry action/receipt 和 execution authority。上层随后以该 authority
恢复本 manifest，并使用新 permission receipt、admission ticket、Runtime lease 与 Run Grant 继续原 batch
的连续 H5a 前缀；不得复活旧 ticket，也不得接受客户端重述原请求。
