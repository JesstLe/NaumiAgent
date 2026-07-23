# UI-17.3a Typed Feature Capability Downgrade

## 1. 目标

关闭“新终端 UI 已认识 typed event，但连接的旧 Bridge 尚不认识”这一真实升级窗口。首个受治理能力是
`evolution_evaluation_lane`：现代双方协商成功时，`/evolution evaluation <comparison-id>` 打开 typed 专页；
能力缺失或用户在 hello 完成前立即输入时，New UI 将同一命令作为普通 `submit` 排队发送，由后端共享 Slash
Router 执行，不发送旧 Bridge 无法识别的 client event。

这是 UI-17.3 的第一个独立能力降级闭环，不把单一事件的治理误称为所有协议事件已经完成兼容分类。

## 2. 协商与执行边界

- `protocol-contract.json` 与 Python `PROTOCOL_CAPABILITIES` 同时发布可选能力
  `evolution_evaluation_lane`；它不是启动 required capability，因此旧 Bridge 的 hello ACK 仍可成功。
- Node reducer 只在 `protocolNegotiated=true` 且 ACK 的交集中包含该能力时建立 typed route、loading 状态并发送
  `evolution/evaluation-lane/request`。
- hello 未完成或能力缺失时，界面显示“兼容模式”中文说明，并调用正常 `submitUserMessage()`；因此消息拥有与普通
  对话相同的 request id、delivery、retry 和断线失败语义，而不是旁路写入 Bridge。
- Python Bridge 在 normalize 之后、任何 executor 或 Store 查询之前检查 client-event capability。未协商却直接发送
  typed request 时返回 `protocol_capability_not_negotiated`，不执行 Evaluation authority，也不泄露内部状态。
- TUI 是 in-process fallback，不进行 JSONL hello；它一直使用同一 Slash Router，因此天然对应兼容路径，不复制
  capability 判断。

## 3. 兼容矩阵

| New UI | Bridge | 行为 | 证据 |
| --- | --- | --- | --- |
| 新 | 新 | 协商 capability，发送 typed request，打开专页 | Node state + Bridge typed tests |
| 新 | 旧 | ACK 无 capability，显示兼容模式并发送 `submit` | 真实 Node UI + `history-bridge.js` 进程测试 |
| 新且用户极早输入 | 任意 | hello 前使用可排队 `submit`，协商失败时不会泄漏到 Bridge | state 与既有 hello send gate |
| 未协商 client | 新 | Bridge 固定错误，executor 零调用 | Python Bridge test |
| TUI | 当前 Engine | 共享 Slash Router 直接执行 | UI-17.2d/TUI golden |

## 4. 验收标准

1. contract、Python 常量与测试对 capability 名称一致，hello payload 自动声明新能力。
2. modern state 只发送 typed request，不产生聊天消息；legacy state 只发送 `submit`，不留下永久 loading route。
3. 真实终端进程连接不声明新能力的旧 Bridge，屏幕出现兼容说明和 Slash 执行结果；debug trace 中存在 `submit`，
   不存在 `evolution/evaluation-lane/request`。
4. Bridge 收到未协商 typed request 时返回稳定错误，executor 不运行。
5. 定向 Python/Node/进程测试、协议注册、Ruff、compile 与 JS syntax 通过；不运行全量测试。

## 5. 诚实边界与后续

UI-17.3 仍是 partial。当前只治理第一个新增 typed feature；Goal/Task 已有各自 Markdown fallback，但 session、
Workbench、Harness detail、permission 等事件尚未统一进入 machine-readable capability-to-event registry。未知 server
informational event 的 attested additive 忽略已由 ARC-03.3a 完成；未知 control/terminal 的局部中止
仍未按 request/run scope 实现。

下一切片应先比较 UI-17.3b 通用 event-capability registry 与 HAR-07.4b sequence/gap recovery 的依赖价值，不应继续
为每个页面手写分散判断。
