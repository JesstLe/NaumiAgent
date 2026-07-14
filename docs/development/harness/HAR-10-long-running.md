# HAR-10 长周期 Harness Orchestration

## 目标

支持小时/天级目标、Agent 集群、浏览器和后台工具的可靠执行：心跳、租约、背压、恢复、
人工交互和最终证据闭环统一，不与 Pursuit 形成两个竞争循环。

## 子模块

- HAR-10.1 Run lease：owner、epoch、expires_at、幂等续租、脑裂拒绝。
- HAR-10.2 Heartbeat：runtime/worker/browser/agent 分层心跳与健康状态。
- HAR-10.3 Durable queue：优先级、公平性、容量、立即发送消息插队规则。
- HAR-10.4 Checkpoint：目标、criteria、todo、budget、evidence cursor、pending interaction。
- HAR-10.5 Resume/reconcile：进程重启后检查外部任务真实状态，不盲目重跑。
- HAR-10.6 Human interaction：结构化选项、自定义输入、超时、暂停和 takeover。
- HAR-10.7 Cluster scheduling：能力、资源、workspace 锁、亲和/反亲和和隔离。
- HAR-10.8 Terminal decision：完成、waiting、blocked、cancelled、budget_exceeded。

## 与 Pursuit 的合并原则

Pursuit 提供目标分解/评估/行动；HAR-10 提供可靠运行控制和完成裁判。Pursuit 不直接持有
worker lease，Harness 不重新实现 goal planner。Goal、Pursuit、Harness Run 通过稳定 ID
关联。

## 验收标准

- 杀死 runtime 后在新进程恢复，不重复已经完成的 destructive action。
- 三个 worker 同抢一租约，只有一个 owner；旧 epoch 的结果被拒绝并审计。
- 1k 工具任务有容量和公平性，无界队列不可出现；取消能传播到 agent/browser/process。
- 等待用户时不消耗模型轮次；立即发送消息能进入下一安全边界而非破坏当前事务。
- 网络/Store/worker 短暂失败后退避恢复，持续失败进入 blocked 并给下一步。
- A5：24 小时 soak、故障注入、重启、背压、心跳丢失和人工接管报告。
