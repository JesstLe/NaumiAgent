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

## 已完成前置

- ARC-01.4b1-4b2d 已将规范 RuntimePaths、Harness/Evolution/ChatRun/Task/Workbench Store 交由 Composition Root
  显式装配。现有 durable run、step、artifact 与 completion receipt 已在成功、失败、取消和终端断连
  场景验证。
- 这只解决 ARC-02.5 的持久化 owner 前置，不等于 cursor/revision/ack/resume 已实现。ARC-02 仍需先
  完成其余 Runtime Resource/Service 边界，再定义不会重复投递 tool result/receipt 的 Event Store 合同。
- HAR-07.4b1 已让 New UI 在空闲边界内重启 stdio Bridge、重新 hello/sequence 协商，并以精确
  session 复用持久 Receipt 恢复；活动运行和未裁决输入仍 fail-closed。该能力证明了前端重连状态机，
  但没有 event cursor、ACK 或 gap resend，不能替代 ARC-02.3/ARC-02.5 的 Runtime Service 权威。
- ARC-02.5a 已为 `completion/receipt` 与 `harness/receipt` 建立 Composition Root 管理的
  `terminal-events.db`，提供跨 Bridge 稳定 `event_id/stream_id/cursor`、事务并发分配、写前提交、
  双摘要完整性校验和每 session 4096 条有界保留。该切片仍没有客户端 ACK、cursor replay、
  窗口外 gap/snapshot 或活动运行恢复，详见
  [ARC-02.5a](./ARC-02-5a-terminal-event-journal.md)。
- ARC-02.5b 已让 New UI 持久保存 session cursor 与稳定 client identity，Bridge 持久接受单调 ACK；
  空闲重连仅在请求位置与服务端 ACK 一致时按 `resume_after_cursor` 补发窗口内缺失回执；ACK
  不一致、stream 变化或窗口外会执行无 cursor 业务快照，且仅在所有业务权威读取成功后建立新基线。
  活动模型流、工具结果、多客户端 slow-client policy 和本地 socket 仍未完成，详见
  [ARC-02.5b](./ARC-02-5b-terminal-event-cursor-recovery.md)。
