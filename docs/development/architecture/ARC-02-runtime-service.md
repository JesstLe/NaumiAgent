# ARC-02 Runtime Service 化

## 目标

将 Python Runtime 从前端进程内对象升级为本地可管理服务，支持多前端连接、恢复、心跳和稳定
控制 API，同时保持单进程 embedded 模式用于测试和 fallback。

## 子模块

- ARC-02.1 Lifecycle manager：start/ready/drain/stop/crash/restart 与 pid/lock。
- ARC-02.2 Local transport：Unix socket 与 Windows named pipe；stdio 保留兼容。
- ARC-02.3 Client session：hello、capability、auth nonce、heartbeat、reconnect。
- ARC-02.4 Command API：submit/cancel/interaction/task/permission/harness/debug。
- ARC-02.5 Event stream：cursor、revision、ack、resume、bounded buffer、slow client。
- ARC-02.6 Multi-client policy：一个控制者、多只读观察者、takeover 审计。
- ARC-02.7 Embedded adapter：测试与 TUI fallback 复用相同 handler。

## 安全

本地 socket 权限收敛到当前用户；nonce 防止同机其他用户连接；不开放 TCP 默认端口。所有命令
仍经过 PermissionChecker，service 化不是权限旁路。

## 验收标准

- 前端杀死/重启后从 cursor 恢复，不重复 tool result 和 receipt。
- Runtime 崩溃给中文诊断，supervisor 有退避和重启上限。
- 两个控制客户端同时提交时只有 lease owner 成功，另一个收到可解释冲突。
- 慢客户端不拖住 Agent；超出 buffer 提示 full snapshot recovery。
- macOS/Linux socket、Windows named pipe、embedded 三模式 contract tests 相同。
- A4：binary/wheel clean install 后服务可启动、升级、回滚。
