# UI-17.3b Event Capability Registry

## 问题

UI-17.3a 首次为 Evaluation Lane 实现 typed event 协商降级，但 Bridge 和 New UI 仍分别写死
`evolution_evaluation_lane` 能力名。每增加一个 Harness、Workbench 或 Permission typed feature 都继续复制
判断，会使发布合同、Bridge 执行门和 Node 降级路径漂移。

## 权威合同

`frontend/terminal-ui/protocol-contract.json` 新增 `event_capabilities`，以能力名为 key，显式列出受其
治理的 `client_events` 和 `server_events`。它是能力与事件关系的唯一发布事实源：

- 能力必须已在 `negotiation.capabilities` 发布；
- 事件必须存在于对应方向的公开 event enum/list；
- 同一方向的事件只能归属一个能力，防止协商语义模糊；
- 绑定不能为空，数组内不能重复；
- registry digest 同时覆盖 event governance policy 和 capability binding，变更可被 Doctor/debug 识别。

Python `ProtocolEventRegistry` 以 frozen typed model 加载 wheel 内同一合同；Bridge 在调用任何 executor
前通过 `required_capability("client", event_type)` 查询。Node 同样先严格验证合同，再通过
`requiredEventCapability()` 决定 typed route 或共享 Slash 降级。

## 当前绑定

| capability | client event | server event | 无能力行为 |
| --- | --- | --- | --- |
| `evolution_evaluation_lane` | `evolution/evaluation-lane/request` | `evolution/evaluation-lane` | New UI 走 `submit`；Bridge 拒绝未协商 typed request |

本切片不给现有事件追加新的能力门，因此不改变 Goal、Task、Workbench、Harness 或 Permission
的当前兼容行为。后续每次扩展必须同时提供 typed path、兼容 path 和新旧 Bridge 证据。

## 验收

- Python 和 Node 从同一 JSON 合同查到相同 client/server 绑定；
- 未发布能力、未注册事件、重复归属、空绑定在启动时 fail closed；
- 已协商 Evaluation Lane 仍只发 typed request，未协商时 executor 零调用；
- 真实 Node 进程连接旧 Bridge 时仍显示中文兼容说明并走 Slash 通道；
- 定向 Ruff、Python protocol/Bridge 测试、Node protocol/state/process 测试和 JS syntax 通过。

## 边界

本切片建立发布注册表与 client request 执行门，不实现 ARC-03.3 的未知事件 criticality
处置，也不实现 ARC-03.5/ARC-02.5 的 sequence、cursor、ack、gap recovery 或 Event Store。因此
UI-17.3 和 HAR-07.4b 仍为 partial。
