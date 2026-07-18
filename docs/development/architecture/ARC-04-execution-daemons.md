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
- 现有 `ValidationExecutor` 只能提供 argv/timeout/process-group/output bound；`CodeExecuteTool`
  在容器不可用时允许本机降级。两者都不能单独证明 Sandbox Eval 的 `no_host_side_effect`，因此
  ARC-04 隔离后端完成前，HAR-08.4 保持 planned，禁止用“临时目录 + subprocess”冒充沙箱。

## 已完成的最小前置

HAR-10.5b 已在本地 BackgroundRunner 接通 caller idempotency key、pre-spawn reservation、同 runtime 并发
去重和重启回执复用。这验证了 ARC-04.2 的 job identity/immutable input 形状，但 `tasks.json` 没有跨进程事务、
permission grant、workspace lease 或 daemon authority，因此 ARC-04.2 仍是 planned，禁止把该前置标记为
Tool daemon 已完成。

HAR-10.5c 又验证了本地 reconcile contract：只有当前 Runner 同时持有活进程与 watcher 才能报告 managed
active；PID 存在但所有权丢失必须报告 orphan 并 fail closed。该合同可作为 ARC-04.6 Supervisor 的输入形状，
但尚不具备 daemon 接管、心跳、跨进程 fencing 或孤儿清理能力。

HAR-10.2a 已提供可复用的 heartbeat snapshot（instance/epoch/sequence/phase/timeout）与机械健康分类，并接入
Pursuit lease worker。这是 ARC-04.1/04.6 的最小协议前置，但还没有 daemon producer、历史丢包统计、
crash-loop/quarantine/drain 或 supervisor 动作，因此 ARC-04 仍保持 planned。
