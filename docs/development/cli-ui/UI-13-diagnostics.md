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
