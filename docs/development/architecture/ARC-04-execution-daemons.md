# ARC-04 Tool、Browser、Agent 执行 Daemon

## 目标

将高风险、长寿命或资源密集执行隔离为 daemon worker；Runtime 负责计划和权限，daemon 负责
受限执行、心跳、日志引用和取消。

## 子模块

- ARC-04.1 Worker contract：capabilities、platform、resource、version、health。
- ARC-04.2 Tool job：immutable request、permission grant id、workspace lease、idempotency key。
- ARC-04.3 Shell worker：PTY/非 PTY、cwd/env allowlist、process tree cancel、artifact log。
- ARC-04.4 Browser worker：profile isolation、tab/run ownership、human takeover、cleanup。
- ARC-04.5 Agent worker：context bundle、tool scope、budget、message channel、terminal result。
- ARC-04.6 Supervisor：heartbeat、crash loop、quarantine、drain、upgrade。
- ARC-04.7 Audit adapter：规范化事件回 Runtime/Harness，不回传 secret/raw 大输出。

## 当前实现状态

- ARC-04.1a 已完成 Worker 身份/版本、跨平台事实、能力与隔离声明、资源容量、健康绑定和 fail-closed
  admission 合同，详见 `ARC-04-1a-worker-contract.md`。
- ARC-04.1b 已完成 Runtime-owned SQLite registration authority、最高 epoch incarnation fencing、撤销、
  authority-only admission 与 Store Catalog/Composition Root 装配，详见
  `ARC-04-1b-worker-registration-authority.md`。
- ARC-04.2a 已完成 execution-scoped grant authority：绑定参数 digest、幂等键、Tool run lease、active Worker
  epoch、权限来源与短期 expiry，并可在消费前重新 fencing，详见
  `ARC-04-2a-scoped-execution-grant-authority.md`。
- ARC-04.2b 已完成 immutable ToolJob admission：同时消费 execution grant、active Worker、实时
  heartbeat/capacity、能力/隔离要求与 Tool lease，并在 dispatch 前重新 fencing，详见
  `ARC-04-2b-immutable-tool-job-admission.md`。
- ARC-04.2c 已完成 ToolJob schema v2 单调 lifecycle receipt、dispatch-before-send、Worker incarnation fencing、
  并发终态幂等、unknown 副作用边界与 v1 migration，详见 `ARC-04-2c-tool-job-lifecycle-receipts.md`。
- ARC-04.3a 已完成认证本地 non-PTY transport、默认断网 OS sandbox、process-tree cancel、资源上限、artifact
  digest，并由 Coordinator 消费 ARC-04.2b/2c 权威链，详见
  `ARC-04-3a-authenticated-non-pty-shell-worker.md`。
- ARC-04.3b 已完成一次性 Shell admission composer，组合精确子授权、Worker incarnation、snapshot Tool lease、
  delegated grant、ToolJob admission 与终态 cleanup，详见 `ARC-04-3b-ephemeral-shell-admission-composer.md`。
- UI-12.3b3 已补充独立的有界 Run Delegation authority：长运行不再依赖放宽父回执 freshness，而是绑定
  父 digest、下游 scope 和 Harness Run Lease fence 并持续复验；ARC-04 下一切片需把该 authority 接入
  短期子回执与 Shell admission，不能让消费者自行续签或拼接授权链。
- UI-12.3b4 已让 schema v4 child receipt 与 ExecutionGrant 在签发和 dispatch 阶段消费 Run Grant，并将
  ExecutionGrant expiry 截断到 child expiry；
- ARC-04.3c 已将共享 Run Grant Store/Authority 接入 Composition Root 与 Shell admission composer，并用真实
  sandbox 命令证明成功路径、用撤销证明 payload 前阻断；详见 `ARC-04-3c-run-delegated-shell-admission.md`。
- 当前 Worker 是每 Job 一个短寿命进程，不是带 heartbeat 的长寿命 daemon；PTY、Supervisor、并发背压与
  Windows 隔离后端仍未完成。因此 ARC-04 保持 partial。

## 验收标准

- 无有效 permission grant 的 job 被 daemon 拒绝；bypass grant 仍有 scope/run/expiry。
- 同 idempotency key 重试不重复 destructive action。
- 取消 shell 清理整个进程树；取消 browser 清理 owned tabs/profile；不影响其他 job。
- worker crash 后 Runtime 判断已执行/未执行/未知，不盲重试未知副作用。
- 100 并发 job 资源上限生效，日志进入 artifact 而非内存堆积。
- daemon 版本不兼容时 drain 并提示升级，不接受新任务。

## 下游硬依赖

- HAR-08.4 Sandbox Eval 只能消费 ARC-04 提供的显式隔离能力合同：临时 workspace/worktree、
  默认断网、环境变量 allowlist、资源上限、进程树取消、artifact digest 与可审计退出状态。
- 现有 `ValidationExecutor` 仍不能证明 Sandbox Eval 的 `no_host_side_effect`。HAR-08.4a/4b 已使生产
  `/harness check` 和 Agent Tool 消费 ARC-04.3a/3b 的真实隔离与委托链；HAR-08.4e 又提供共享的成组
  Check execution kernel，HAR-08.4f 负责连续 Batch lease/grant/恢复/清理。通用 surface 仍不得直接复用旧
  subprocess 路径冒充 Sandbox。

## 已完成的最小前置

HAR-10.5b 已在本地 BackgroundRunner 接通 caller idempotency key、pre-spawn reservation、同 runtime 并发
去重和重启回执复用。这一早期前置只验证了 identity 形状；ARC-04.2a-2c 现已补齐 grant、lease、immutable
admission 与 lifecycle receipt，但 `tasks.json` 仍不是 daemon authority，禁止把 BackgroundRunner 标记为
Tool daemon。

HAR-10.5c 又验证了本地 reconcile contract：只有当前 Runner 同时持有活进程与 watcher 才能报告 managed
active；PID 存在但所有权丢失必须报告 orphan 并 fail closed。该合同可作为 ARC-04.6 Supervisor 的输入形状，
但尚不具备 daemon 接管、心跳、跨进程 fencing 或孤儿清理能力。

HAR-10.2a 已提供可复用的 heartbeat snapshot（instance/epoch/sequence/phase/timeout）与机械健康分类，并接入
Pursuit lease worker。这是 ARC-04.1/04.6 的最小协议前置，但还没有 daemon producer、历史丢包统计、
crash-loop/quarantine/drain 或 supervisor 动作；在 ARC-04.1a 交付前，ARC-04 因此保持 planned。

ARC-04.1a 在该 heartbeat 之上增加了能力、平台、资源、隔离和容量合同，并验证 worker/instance/epoch 与
heartbeat generation 一致。它没有复制 liveness 状态机，也没有放宽上述 daemon producer 与 supervisor 缺口；
ARC-04 当前状态为 partial (4.1a, 4.1b, 4.2a, 4.2b, 4.2c, 4.3a, 4.3b)。
