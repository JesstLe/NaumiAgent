# HAR-10 长周期 Harness Orchestration

## 目标

支持小时/天级目标、Agent 集群、浏览器和后台工具的可靠执行：心跳、租约、背压、恢复、
人工交互和最终证据闭环统一，不与 Pursuit 形成两个竞争循环。

## 子模块

- HAR-10.1 Run lease（partial）：
  - HAR-10.1a 已实现：workspace/run-kind 隔离的 owner、单调 epoch、expires_at、幂等 acquire/renew、
    release 后 epoch 保留，以及旧 epoch 结果拒绝审计；见 [设计](HAR-10-1a-run-lease-design.md)。
  - HAR-10.1b 已实现：Pursuit 启动 admission、keepalive、动作/结果/终态 fencing、可靠释放和 live-owner
    resume 拒绝；见 [设计](HAR-10-1b-pursuit-lease-integration.md)。
  - 未完成：browser/background/agent/runtime 的逐域接入，以及跨 Store 原子提交。
- HAR-10.2 Heartbeat：runtime/worker/browser/agent 分层心跳与健康状态。
- HAR-10.3 Durable queue：优先级、公平性、容量、立即发送消息插队规则。
- HAR-10.4 Checkpoint（partial）：
  - HAR-10.4a 已实现：严格有界 schema、单调 sequence、canonical JSON + SHA-256、篡改拒绝、Pursuit
    安全边界写入，以及 legacy missing / verified ready 恢复判定；见
    [设计](HAR-10-4a-pursuit-checkpoint-core.md)。
  - HAR-10.4b 已实现：GoalSpec/history/budget 重建、新 lease epoch continuation、累计预算门、
    `planned/action_inflight/action_result` 阶段协议，以及副作用不明确时 fail closed；见
    [设计](HAR-10-4b-pursuit-resume-executor.md)。
- HAR-10.5 Resume/reconcile（partial）：
  - HAR-10.5a 已实现：Pursuit shell/background 的稳定行动 identity、不可变哈希事件链、单调状态机、
    派发前账本、后台 task ID 关联、终态回收、并发/篡改拒绝和重复派发阻断；见
    [设计](HAR-10-5a-pursuit-action-ledger.md)。
  - 未完成：BackgroundRunner caller idempotency key、`reconcile_required` 自动解除，以及 browser/agent/API
    外部状态核对。
- HAR-10.6 Human interaction：结构化选项、自定义输入、超时、暂停和 takeover。
- HAR-10.7 Cluster scheduling：能力、资源、workspace 锁、亲和/反亲和和隔离。
- HAR-10.8 Terminal decision：完成、waiting、blocked、cancelled、budget_exceeded。
  - HAR-10.8a 已实现：assessment 去除隐式全量探针，criterion 与模型 action 共用定向验证策略，广域
    pytest/ruff/tox/nox 及主流语言测试入口 fail closed；见
    [设计](HAR-10-8a-scoped-verification-policy.md)。

## 与 Pursuit 的合并原则

Pursuit 提供目标分解/评估/行动；HAR-10 提供可靠运行控制和完成裁判。Pursuit 不直接持有
worker lease，Harness 不重新实现 goal planner。Goal、Pursuit、Harness Run 通过稳定 ID
关联。

## 已完成前置

ARC-01.4b2e 已把 GoalStore/PursuitStore 的规范路径、lazy initialization 和运行时资源所有权收口，
并验证 Goal 的 `pursuit_run_id` 在 Store 重开后仍能恢复对应 PursuitRun。下一步 UI 可以读取类型化权威
状态，不需要解析 `/goal` 或 `/pursue` 文本。

ARC-01.4b2e 只解决资源归属和稳定引用。HAR-10.1a 已在 Harness schema v11 交付通用 lease/epoch 与结果
fencing authority，HAR-10.1b 已完成 Pursuit 首个生产接入，HAR-10.4a/4b 已交付权威 checkpoint 与安全
continuation，HAR-10.5a 已让 shell/background 行动在外部派发前进入持久账本；caller idempotency、
browser/agent/runtime 逐域接入、跨 Store 原子性、heartbeat、interaction queue 和自动 reconcile 仍属于后续实现。

## 验收标准

- 杀死 runtime 后在新进程恢复，不重复已经完成的 destructive action。
- 三个 worker 同抢一租约，只有一个 owner；旧 epoch 的结果被拒绝并审计。
- 1k 工具任务有容量和公平性，无界队列不可出现；取消能传播到 agent/browser/process。
- 等待用户时不消耗模型轮次；立即发送消息能进入下一安全边界而非破坏当前事务。
- 网络/Store/worker 短暂失败后退避恢复，持续失败进入 blocked 并给下一步。
- A5：24 小时 soak、故障注入、重启、背压、心跳丢失和人工接管报告。
