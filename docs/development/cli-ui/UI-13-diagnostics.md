# UI-13 Doctor/Debug 全屏诊断

## 目标

将现有 Markdown doctor 和 debug trace 升级为可复制、可筛选、可实时刷新且保护隐私的诊断页。

## 子模块

- UI-13.1 Health model：runtime/model/provider/store/git/node/browser/MCP/terminal。
- UI-13.2 Severity：ok/degraded/error/unknown，区分用户配置与产品缺陷。
- UI-13.3 Live probes：显式启动、预算与超时，不在打开页面时偷偷联网。
- UI-13.4 Trace viewer：event type、run/call/task id、时间、错误，正文默认折叠。
- UI-13.5 Export：脱敏诊断包、manifest、digest、用户预览。
- UI-13.6 Repair actions：只提供安全可逆动作，外部安装/删除必须确认。

## 验收标准

- 无 API key 时解释配置位置和下一步，不触发 Keychain 反复授权。
- Store 损坏、Node 过旧、Bridge 无心跳、provider 401/404/429/5xx 可区分。
- 导出包不含 secret、完整环境、用户正文、raw reasoning；自动扫描 fixture。
- live probe 取消后所有子进程/连接释放。
- 诊断页自身失败仍有纯文本 fallback 和日志路径。

## 分阶段实现

- UI-13.1a Typed 本地 Health 状态页：已实现。现有 Doctor 本地检查被转换为 bounded typed
  runtime/model/provider/store/git/node/browser/MCP/terminal 状态，新 UI 合并真实 Bridge heartbeat 并支持
  刷新/滚动/返回；Markdown fallback 保留，且页面不会偷偷运行 live provider probe。实现与验收见
  `UI-13-1a-typed-local-health.md`。
- UI-13.1b Worker Authority Health：已实现。Doctor 以严格只读方式组合 Worker Registry active contract 与
  Harness heartbeat，显示 epoch、平台、合同容量和可信活性；缺失、陈旧、身份不匹配、损坏与未来 schema
  均有 fail-closed 中文结论。新 UI 与 TUI 复用同一检查，详见 `UI-13-1b-worker-authority-health.md`。
- UI-13.1c Runtime Heartbeat Retention Health：已实现。New UI 投影真实 Bridge 调度状态，TUI fallback 复用
  同一 item 并明确标记进程内状态不可观测；两端均不在 Doctor 打开时触发清理，详见
  `UI-13-1c-runtime-heartbeat-retention-health.md`。
- UI-13.1d Worker Capacity Health：已实现。共享只读 authority 从 Worker Registry v3 投影真实 reservation
  占用/可用槽位，按 assessed time 逻辑忽略过期项但不写回；New UI/TUI 显示同一中文容量事实，详见
  `UI-13-1d-worker-capacity-health.md`。
- UI-13.1e Worker Queue Backlog Health：已实现。相同只读 authority 投影 durable policy、live waiting、
  active claim、oldest wait 与待收口过期事实；队列满只降级，identity/schema/link 损坏仍 fail closed，
  New UI/TUI 不维护第二份队列状态，详见 `UI-13-1e-worker-queue-backlog-health.md`。
- UI-13.2a Provider Diagnostic Codes：已实现。Doctor 以低基数稳定码区分凭据、配置、404、429、5xx、
  timeout 与连接错误；结构化 HTTP 状态优先，New UI/TUI/CLI 复用同一 typed authority，详见
  `UI-13-2a-provider-diagnostic-codes.md`。
- UI-13.3a Bounded Provider Live Probe：已实现。共享 authority 固定最多 1 个请求、8 个输出 token、
  默认 15 秒超时且无自动重试；New UI 提供 `p/c` 启动与取消、Textual TUI 提供
  `/doctor probe [timeout-ms|cancel]`，CLI 与 Agent Tool 复用相同动作，详见
  `UI-13-3a-bounded-provider-live-probe.md`。
- UI-13.4a Typed 隐私有界 Trace 索引：已实现。共享 authority 只读取单轮 DebugTrace 最近 2 MiB，
  将正文、模型输出、reasoning、工具参数和异常内容折叠为 typed 元数据；New UI、TUI、CLI 与 Agent
  Tool 复用同一筛选、限制和 Snapshot 语义，详见 `UI-13-4a-typed-trace-index.md`。
- UI-13.5a Typed 脱敏诊断包导出：已实现。共享 authority 构建固定 3 文件、最大 512 KiB 的确定性 ZIP，
  先展示文件/大小/digest/隐私边界，再以进程内 Plan 和精确 Snapshot 摘要原子写入平台状态目录；
  New UI、TUI、CLI fallback 与 Agent Tool 复用同一实现，详见
  `UI-13-5a-typed-diagnostic-export.md`。
- Provider 探测历史/SLO、Trace 实时追尾/历史选择/授权展开、可选 trace attachment、Windows DACL
  显式校验与修复动作仍为
  planned；不得把 UI-13
  整体标记为 implemented。
