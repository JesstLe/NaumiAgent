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
- provider 稳定错误码、显式 live probes、Trace viewer、脱敏导出与修复动作仍为 planned；不得把 UI-13
  整体标记为 implemented。
