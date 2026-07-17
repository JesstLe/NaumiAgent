# HAR-06.5b2b Retention 长期入口生命周期

## 交付边界

本切片只把 HAR-06.5b2a 的默认关闭周期核心接入真实长期运行进程，不扩展到 HAR-10 的通用
daemon、集群或 24 小时任务编排。统一启动顺序为：

`创建 Engine → 恢复 Session 协调 → 按配置启动 retention worker → 对外 ready`。

恢复失败时 worker 不得启动；入口必须关闭已经创建的 Engine。一次性 `naumi run <task>` 只执行
恢复和用户任务，不启动周期 worker，避免短进程在任务执行期间竞争后台清理权威。

## 入口矩阵

| 入口 | 长期运行 | 启动方式 | 关闭方式 |
| --- | --- | --- | --- |
| 默认 New UI Python Bridge | 是 | `create_bridge()` 在返回 Bridge 前完成统一启动 | Bridge shutdown drain Engine |
| Textual TUI fallback | 是 | mount 后的 startup worker 完成统一启动 | unmount drain Engine |
| deprecated 旧 CLI | 是 | 进入交互循环前完成统一启动 | finally drain Engine |
| FastAPI | 是 | lifespan 在 `yield` 前完成统一启动 | lifespan finally drain Engine |
| `naumi run <task>` | 否 | 仅 `recover_session_reconciliations()` | 单任务 finally shutdown |

统一入口由 `AgentEngine.start_long_running_services()` 提供。它先等待持久协调恢复成功，再调用受
`periodic_enabled` 配置门控制的 worker start；因此默认配置不会产生后台清理。

## 状态与事件

- `AgentEngine.session_retention_worker_status()` 返回有界、JSON-safe 的同一状态结构；
- New UI 的 `ready` 和完整 `runtime/status` 包含 `retention_worker`；
- Bridge 用心跳 `ping` 检测 worker 快照变化，仅在状态或计数变化时于 `pong` 后追加一条
  `runtime/status`；静稳期不触发重复渲染，前端也无需另建轮询或状态源；
- Node normalizer 对状态枚举、布尔值、非负计数/时长和 256 字符文本上限做失败关闭校验；
- FastAPI `/api/v1/health` 返回同一字段集合的类型化 `retention_worker`；
- 原始异常不进入状态；Bridge 状态读取故障降级为封闭的 `status_unavailable`。

## 故障与关闭语义

- New UI Bridge 启动恢复失败时先 `engine.shutdown()` 再传播启动失败；
- FastAPI lifespan 用统一 `finally` 覆盖 `yield` 前失败和正常退出，权限 broker 与 Engine 均会关闭；
- TUI 恢复失败保持 worker 停止并显示中文恢复失败状态；
- Engine shutdown 复用 6.5b2a 的 cancel/drain/release，当前协调先进入安全持久状态，再释放 owner
  条件租约；
- 多入口/多进程同时启用仍只有 Harness DB v7 租约 winner 执行周期 pass。

## 验收证据

- Engine 单测锁定 recover-before-start，并证明恢复异常不会调用 start；
- New UI Bridge 测试证明创建时统一启动、失败关闭 Engine、心跳发出当前 worker 状态；
- TUI 与 API 生命周期测试证明入口调用统一启动，API 启动失败不会泄漏 Engine；
- 一次性任务测试证明只恢复、不调用长期服务启动；
- 真实 Engine + Session Store + Harness Store 场景启用周期配置后执行首轮，shutdown 后新 owner 可立即
  获取并释放租约；
- Python 聚焦测试、Node 协议测试、ruff、py_compile 和 diff check 通过。

## 后续依赖

HAR-06 完成后，HAR-10 可以复用本切片的“恢复后启动、心跳状态、关闭 drain”语义，但不得把
retention 的单行租约直接冒充通用 Agent/Browser/Tool 集群调度。HAR-10 仍依赖 HAR-08 与 ARC-06。
