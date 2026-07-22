# ARC-03.5a JSONL 序号完整性隔离

## 目标

在 Event Store、cursor 和 snapshot 补发尚未交付前，先保证默认 New UI 不会把缺失、重复或乱序的
Bridge 事件静默折叠为“看似可信”的本地状态。本切片建立可协商的序号完整性能力、真实写出顺序和
fail-closed 隔离路径，为 ARC-03.5 完整恢复、HAR-07.4b 与长周期 Harness 可观测性提供共同前置。

## 权威边界

- Python `JsonlEngineBridge.emit()` 在同一个异步锁内分配序号、编码并写出；因此序号与 stdout 的实际
  观察顺序一致，并发调用不会得到“先编号、后抢锁”造成的倒序。
- 发布合同声明可选能力 `sequence_integrity`。新旧 Bridge 未协商该能力时仍按既有兼容行为运行；它
  不是某个单独事件的 feature gate，因此不进入 `event_capabilities`。
- Node 在协商前只观察启动事件。hello ACK 声明支持后，守卫追溯检查已观察的启动序列，并从 ACK
  继续要求正安全整数与严格连续序号。
- 连续事件进入 reducer；重复或倒序事件不重复 reduce，并写入本地审计；缺失、非法或跳号使整个
  JSONL 流进入隔离态，后续事件不再进入 reducer。

## 用户恢复路径

发生缺口时 New UI 先 flush 已验证的批次，输出不含秘密的 expected/received/last sequence 审计，随后
显示中文“当前运行状态待确认”提示并以退出码 1 安全退出。默认 Python 启动器沿用既有一次性回退逻辑，
在同一工作区启动 Textual TUI；用户可用 `/resume` 从持久权威状态核对。TUI 直接消费 Engine，不经过
JSONL，因此不复制一套序号状态机。

## 验证

- 反转前两个并发锁竞争者，证明 stdout 观察到的序号仍为 `1, 2`，而 payload 顺序确实被反转。
- Node 纯守卫覆盖协商前启动序列、连续、重复、倒序、缺失、非法、跳号与隔离后的事件。
- 真实子进程 fixture 协商能力后跳过序号 2；New UI 不渲染 seq=3 的 payload，写入 desync 审计并以
  退出码 1 结束。
- 真实 Python Bridge 启动链验证协商前 `ready/debug` 与 hello ACK 之间保持连续并正常进入欢迎页。
- 现有启动器单元测试证明非零 New UI 退出只回退一次到 Textual TUI。

## 明确未完成

本切片不提供 global cursor、事件持久化、ACK、重连去重、snapshot 请求或自动补发，不能把
ARC-03.5、HAR-07.4b 或 UI-17 发布门标记为完成。完整恢复必须等待 ARC-02 Event Store 与 Runtime
Service 的 cursor/snapshot authority，不能用进程内缓存伪装。
