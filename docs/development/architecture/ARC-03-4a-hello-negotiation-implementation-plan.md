# ARC-03.4a 启动协议协商实施计划

## 目标

以一个 RED→GREEN 切片完成 Python Bridge 与 Node 新 UI 的显式 hello 协商，并产生 HAR-08 可复用的
兼容性契约，不扩展到 ARC-03 其他子模块。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `frontend/terminal-ui/protocol-contract.json` | 协商版本区间、公开能力及 required capability 权威清单 |
| `src/naumi_agent/ui/protocol.py` | contract 读取、hello 标准化、版本与能力协商 |
| `src/naumi_agent/ui/bridge.py` | hello 成功 ACK、失败 typed error、阻止失败后的 status |
| `frontend/terminal-ui/src/protocol.js` | contract 校验、hello 构造、ACK 协商结果校验 |
| `frontend/terminal-ui/src/index.js` | 启动握手与发送门控 |
| `frontend/terminal-ui/src/state.js` | 协商完成状态与失败状态 |
| `tests/unit/test_ui_bridge.py` | Python 边界与 Bridge 行为 |
| `frontend/terminal-ui/test/protocol.test.js` | Node contract/normalizer 行为 |
| `frontend/terminal-ui/test/index-process.test.js` | UI 进程成功与拒绝场景 |
| `frontend/terminal-ui/test/fixtures/python-bridge-fixture.py` | 真实 Python Bridge 集成 fixture |

## Task 1：RED — 固定协商契约

- 为 Python 写 version range、capability intersection、legacy hello 和 typed failure 测试；
- 为 Node 写 contract validation、hello payload 和 hello ACK normalization 测试；
- 只运行两个协议测试文件，确认因缺少实现失败。

## Task 2：GREEN — Python 协商边界

- 从打包的 `protocol-contract.json` 读取并验证 negotiation；
- hello payload 标准化时拒绝 bool version、反向区间、非法能力；
- 实现纯函数 `negotiate_hello()`，返回不可变、确定性结果；
- Bridge 对两个失败类别发 typed error，不发送 ACK/status；
- 保留旧 hello 一个发布周期。

## Task 3：GREEN — Node 启动门控

- `createHelloPayload()` 只从 contract 生成公开字段；
- `normalizeServerRecord()` 严格验证 hello negotiation ACK；
- hello ACK 前，交互发送进入本地队列；成功后按原顺序释放；
- 协商错误清空待发队列并显示中文可行动提示；
- heartbeat 只在协商成功后启动。

## Task 4：真实链路与自审

- 用真实 Python fixture 启动 Node UI，检查双方 debug log 的 selected version 和 capabilities；
- 用不兼容 Bridge fixture 检查没有 submit/status 泄漏；
- 运行定向 Python/Node 测试、Ruff 与 compileall；
- 审视旧客户端兼容是否过宽、错误是否可行动、门控是否可能死锁；
- 把证据和未完成项回写 ARC-03 主文档，独立提交并推送 main。

## 明确不做

- 不升级 envelope version；
- 不引入 semver 解析库；
- 不实现 schema registry/codegen；
- 不改变事件 sequence、snapshot gap recovery 或持久化格式；
- 不运行全量测试。

## 完成状态

- Task 1：完成；RED 以 Python 缺少 `ProtocolNegotiationError` 的导入失败固定。
- Task 2：完成；Python hello 标准化、纯协商函数和 typed Bridge error 已实现。
- Task 3：完成；Node contract hello、ACK 校验、输入门控和 ACK 后 heartbeat 已实现。
- Task 4：完成；真实 Python Bridge 与不兼容 fixture 均完成进程级验证，自审记录在设计文档。
