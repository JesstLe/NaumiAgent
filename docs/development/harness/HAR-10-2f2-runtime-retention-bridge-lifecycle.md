# HAR-10.2f2 Runtime Retention Bridge Lifecycle

## 目标

把 HAR-10.2f1 周期核心接入默认 New UI Python Bridge，使 runtime heartbeat 诊断记录以安全默认值自动治理，同时不让
清理故障影响模型运行、heartbeat producer 或 Bridge 关闭。

## 配置合同

`AppConfig.harness.runtime_heartbeat_retention` 提供：

- `enabled=true`：默认启用，但只处理 runtime heartbeat 诊断行，不删除会话、消息、任务或用户文件；
- `retention_days=7`：范围 3-365 天；
- `interval_seconds=21600`：默认每 6 小时运行一轮；
- `standby_retry_seconds=60`、`lease_seconds=60`；
- `scan_limit=100`、`catalog_limit=200`。

所有数值由 Pydantic 和 HAR-10.2f1 policy 双重限制。示例配置已写入 `config.yaml.example`，可显式关闭；环境变量沿用
AppConfig 的嵌套规则，例如 `NAUMI_HARNESS__RUNTIME_HEARTBEAT_RETENTION__ENABLED=false`。

## Bridge 生命周期

1. Composition Root 构造共享 `TerminalRuntimeLifecycleFactory`，Bridge 不直接构造 producer、policy 或 retention；
2. `emit_ready()` 创建隔离的 `new_ui` lifecycle，并按统一状态机启动 heartbeat 与可选 retention；
3. 当前 Bridge subject ID 始终注入保护集合；
4. ready 与后续 ping 的 `runtime/status` 暴露 configured/state/cycle/deleted/failure/error/time/delay；
5. 同一次 ping 的 Session retention 与 runtime heartbeat retention 变化合并为一条 status，避免重复刷新；
6. shutdown 通过 lifecycle 先 graceful stop retention，再写 producer draining/terminal，保证当前实例不会在收尾竞争中被清理；
7. retention stop 异常记录为稳定错误码，不阻断 engine shutdown 与 heartbeat terminal；配置状态来自 factory 快照，
   不受 Engine config 后续修改影响。

显式禁用或 heartbeat producer 启动降级时不会留下后台清理任务。缺少 Composition-owned factory 的测试替身保持
无副作用；状态仍明确返回 `configured_enabled` 与 `stopped`，不会把“未运行”伪装成成功清理。

## 验收证据

- 配置默认值与 3 天最小边界通过定向 Pydantic 测试；
- 真实 Bridge + Harness SQLite 启动 producer 和 retention，删除 2000 年的 stopped runtime；
- 当前 Bridge heartbeat 保留，shutdown 后 retention 为 stopped、heartbeat 为 terminal；
- 既有 heartbeat graceful/failure/degradation 测试保持通过；
- ping 状态变化仍只发一条 `runtime/status`；
- 静态检查证明 Bridge 不再构造 producer/retention，真实 Composition Root 到 Bridge 的 SQLite 路径通过；
- 只运行 config、runtime retention 与 Bridge 指定测试节点，不运行全量测试。

## 未完成

UI-13.1c 已把 typed `runtime/status` 投影到 New UI Doctor；ARC-01.4c3 随后让 TUI 消费同一 lifecycle 并提供真实
retention snapshot。历史清理详情与手动 wake 控件仍未实现，控制逻辑不得复制到前端。browser/agent producer、
Supervisor 动作和 heartbeat 历史趋势也不属于本切片。
