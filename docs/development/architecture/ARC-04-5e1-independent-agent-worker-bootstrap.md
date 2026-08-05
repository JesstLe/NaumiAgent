# ARC-04.5e1 / HAR-10.7h1 独立 Agent Worker 认证启动前置

## 1. 目标与依赖裁决

HAR-10.7g 完成 publication poison record 隔离后，下一候选是 exact quarantine requeue 或独立 Agent
Worker。当前 publication 已能自动恢复并隔离失败记录，而 Agent 仍完全由 embedded Runtime 执行；因此本轮
选择独立 Worker 的最小真实前置，但不直接扩张完整 scheduler。

本切片必须同时满足两条约束：

1. 不能把 embedded Runtime 注册成独立 Worker，否则多个 Runtime 会互相 takeover/fencing；
2. 不能仅写一条 Registry 记录冒充 OS 进程，注册前必须有真实子进程和本机认证传输证据。

交付物是一个真实、独立、可持续心跳、可排空停止的 Agent 控制进程。它还不消费 Agent Job，因而合同只
声明 `agent_control_transport`，不声明 `agent_context_scope`，健康报告始终
`accepting_jobs=false`。任何需要真实 Agent 执行能力的 admission 都会机械拒绝该进程。

## 2. 不可伪造的能力边界

`WorkerCapability` 增加 Agent 专用的 `agent_control_transport`：

- 证明独立 OS 进程、本机认证 IPC 和 lifecycle control 已存在；
- 不证明 prompt/context 已安全传输；
- 不证明模型、Tool、Permission、Budget 或 Agent Job 已由该进程消费；
- 不证明 workspace/network/resource isolation 已启用。

完整 Agent dispatch 仍要求 `agent_context_scope`。当前 bootstrap 合同故意不包含该能力，且
`health_report()` 固定返回 `active_jobs=0, accepting_jobs=false`。`bypass` 不能增加能力、改变健康报告或
跳过 Worker admission。

## 3. 本机认证传输

`AuthenticatedAgentWorkerProcess` 使用 `multiprocessing` 的 spawn context 创建非 daemon 子进程：

- Windows 使用 authenticated named pipe；
- macOS/Linux 优先使用 runtime-owned 0700 目录下的 Unix socket；
- Unix socket 路径过长时只回退到 loopback TCP；
- 每次启动生成 256-bit authkey 和一次性 nonce，二者不持久化、不进入日志或 UI；
- 子进程 `hello/running/pulse/draining/stopped` 全部精确绑定 protocol、nonce、worker、instance、epoch、
  contract digest、真实 PID、单调 sequence 和 `accepting_jobs=false`；
- 未知字段、缺字段、nonce/PID/digest/sequence 漂移均拒绝，父进程不会尝试兼容猜测。

父进程只在 authenticated `hello` 后调用 `WorkerRegistryStore.register()`。注册成功后才写 STARTING
heartbeat 并向子进程发送 `registered`；子进程随后回执 RUNNING。注册冲突的进程收到 abort 并退出，不会
污染当前 active Worker 的 heartbeat。

## 4. Incarnation 与生命周期

生命周期严格为：

```text
created -> starting -> running -> draining -> stopped
                         \---------------> failed
```

- epoch 从同 worker 的 durable registration history 机械递增；
- 如果已有 active incarnation，启动直接失败，不允许用“更高 epoch”隐式接管健康未知的旧进程；
- 后续 Supervisor 必须先使用 heartbeat/owner lease 证明 takeover 合法，再显式 fencing；
- 运行中 heartbeat 只由 authenticated child pulse 驱动，父进程定时器不能伪造子进程存活；
- 优雅关闭先接收 draining/stopped，再等待 OS 进程真实退出，最后精确 revoke registration；
- EOF、异常退出、非法消息或 shutdown 不完整会写 FAILED heartbeat、撤销精确 incarnation，并终止残留进程；
- 父进程崩溃时子进程会因控制连接 EOF 退出；Registry 留存的 active 事实随后变 stale/offline，等待未来
  Supervisor 裁决，不自动重放 Job。

## 5. Composition 与路径所有权

`RuntimePaths.agent_worker_runtime_dir` 固定为
`runtime_data_dir/agent-worker/transport`。Composition Root 创建惰性的
`AgentWorkerProcessFactory` 并注入 `RuntimeServices`/`AgentEngine`：

- 构造 factory 不创建目录、数据库、socket 或进程；
- 只有显式 `factory.create().start()` 才创建真实运行资源；
- factory 复用 Runtime-owned Worker Registry、Harness Store、workspace 与
  `safety.max_parallel_agents` 合同上限；
- 本切片不在 New UI/TUI 启动时默认拉起 control-only Worker，避免无任务消费能力的常驻进程。

## 6. New UI / TUI 共享诊断

现有 Doctor authority 是 New UI 与 Textual TUI 的共享事实源。只读投影新增合同 capabilities 与
`dispatch_ready`：

- control-only Agent Worker 显示“控制通道就绪、任务调度未开放”；
- 状态为受限，而不是健康可调度；
- 建议明确说明任务仍由 embedded Runtime 执行；
- 不显示 authkey、nonce、PID、socket、workspace、heartbeat detail code 或原始合同；
- 完整 Agent Worker 未来只有在合同声明 `agent_context_scope` 且 admission 通过后，才显示容量可用。

Node New UI 与 Textual TUI 不新增第二套判断；两端继续消费同一个 typed Doctor snapshot/Markdown
fallback。

## 7. 聚焦验收证据

- 构造完全惰性，错误相对路径和 heartbeat 参数 fail closed；
- 真实 spawned PID 与父进程不同，认证后才出现 active Registry 记录；
- 子进程连续 pulse 推进同一 instance/epoch 的 heartbeat sequence；
- 伪造 nonce 的生命周期消息被拒绝；
- `agent_context_scope` admission 同时得到 capability missing 与 not accepting；
- Doctor 显示 control-only/dispatch-disabled，不显示虚假可用槽位；
- 第二实例不能隐式 takeover；第一实例优雅撤销后，新实例使用更高 epoch；
- 子进程被强制终止后写 FAILED heartbeat 并撤销 active registration；
- 优雅停止写 DRAINING/STOPPED、等待 OS 进程退出、撤销 registration、清理 socket；
- macOS/Linux runtime transport 目录权限为 0700；Windows 使用 authenticated pipe 分支。

只运行以下小模块，不运行全量测试：

```bash
python3 -m ruff check \
  src/naumi_agent/daemons/agent_worker_process.py \
  src/naumi_agent/daemons/worker_contract.py \
  src/naumi_agent/daemons/worker_authority_health.py \
  src/naumi_agent/runtime/paths.py \
  src/naumi_agent/runtime/services.py \
  src/naumi_agent/runtime/composition.py \
  src/naumi_agent/ui/doctor.py \
  tests/unit/test_agent_worker_process.py \
  tests/unit/test_runtime_services.py \
  tests/unit/test_runtime_composition.py \
  tests/unit/test_worker_contract.py \
  tests/unit/test_worker_authority_health.py

python3 -m pytest -q \
  tests/unit/test_agent_worker_process.py \
  tests/unit/test_runtime_services.py \
  tests/unit/test_runtime_composition.py \
  tests/unit/test_worker_contract.py \
  tests/unit/test_worker_authority_health.py
```

## 8. 自我审视与未完成边界

本切片真实建立了独立 OS 进程、认证 transport、registration、child-driven heartbeat、drain 和 revoke，
但没有完成独立 Agent 执行：

- 没有 AgentJob owner lease/claim、加密 request/context transport 或 result publication；
- 没有使用 Worker Registry capacity reservation/FIFO；
- 没有 Supervisor、合法 stale takeover、crash-loop budget、quarantine 或 upgrade drain；
- 没有 provider/token/cost reservation 和 workspace/provider fairness；
- 没有默认后台启动策略、用户 start/stop 动作或独立打包后的进程入口；
- Windows named pipe 分支已按相同协议实现，但当前轮只在真实 darwin/arm64 主机运行；Linux/Windows
  打包矩阵仍需 CI/真实主机证据；
- 进程当前不接触用户数据，因此没有宣称 context isolation 或 resource enforcement。

下一步应比较 `ARC-04.5e2` 的 exact AgentJob owner lease + encrypted dispatch 与 `ARC-04.6a` 最小
Supervisor owner/fencing。因为自动 dispatch 不能缺少合法 takeover 和 crash 收口，优先完成能让一个 Worker
安全持有一个 durable Job 的最小 owner lease，不直接实现完整多 Worker scheduler。
