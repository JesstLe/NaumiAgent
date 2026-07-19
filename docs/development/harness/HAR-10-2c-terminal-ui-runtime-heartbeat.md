# HAR-10.2c Terminal UI Runtime Heartbeat

## 目标

让默认 New UI 的 Python Bridge 成为 HAR-10.2 typed heartbeat 的真实 producer。此前前端 `ping/pong` 只能证明
JSONL 连接仍响应，不能跨进程重启读取，也不能区分 graceful stop、写入故障与进程失联。

## Producer 合同

`RuntimeHeartbeatProducer` 复用 `HarnessStore.record_heartbeat()`，不创建第二套状态表。每个 Bridge 进程生成
独立的 `runtime` subject/instance identity，epoch 固定为 1，sequence 在该实例内严格递增：

1. Bridge 发送 `ready` 前提交 `starting` 与 `running`；
2. ready 后每 10 秒提交 `running/runtime_alive`，timeout 为 30 秒；
3. shutdown 入口先停止周期任务并提交 `draining`；
4. Engine 正常关闭后提交 `stopped`；关闭失败则提交 `failed` 并保留原异常语义。

独立 subject 允许同一 workspace 的多个 UI 实例并存，旧实例不能覆盖新实例，heartbeat 也不授予工具执行、恢复、
takeover 或提交结果的权限。周期写失败后 producer 停止刷写，让最后一条记录按机械阈值自然变为 stale/offline；
Bridge 只显示一次中文降级提醒，当前对话仍可继续。

## 生命周期与错误边界

- startup 首次写失败：先发送 `ready`，再提示“运行时心跳降级”，不让可选诊断能力阻断默认 UI；
- background pulse 失败：错误正文、路径和数据库信息不进入协议，只发固定提示；
- 重复 close 不重复 terminal 写；draining 后正常/失败终态只能由 Bridge shutdown 路径选择一次；
- crash/SIGKILL 无法写 terminal，最近 running 会依次变 stale/offline，这正是 crash 可观测事实；
- heartbeat 不是 lease。后续 Supervisor 不得仅凭 offline 自动接管或杀进程。

## 验收证据

- 真实 SQLite 跨 Store reopen 得到 `starting → running → running → draining → stopped` 的最终 sequence；
- Engine shutdown 失败路径得到 `failed/runtime_shutdown_failed`；
- 周期 Store 写失败只触发一次 callback，producer 可在进程最终关闭时补写 terminal；
- 真实 `JsonlEngineBridge.emit_ready()` 后 durable heartbeat 为 running，`shutdown()` 后为 stopped；
- startup Store 故障不阻止 ready，只产生一条无敏感信息的 system notice；
- cadence 边界、Ruff、compileall 和相关小模块测试通过，不运行全量测试。

## 当前边界

本切片不提供通用 runtime worker 列表、历史 jitter/丢包统计、崩溃实例 retention、Supervisor 动作或配置项。
每次 Bridge 启动保留一个 latest subject 行。HAR-10.2d 已提供只删除 terminal/offline runtime 的有界 Store
authority，但尚未交付 worker catalog、默认保留期和周期调度；在这些策略接入前不能宣称 24 小时/长期进程
churn 的存储 SLO 已完成。
