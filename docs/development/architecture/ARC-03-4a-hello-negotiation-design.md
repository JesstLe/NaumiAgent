# ARC-03.4a 启动协议协商设计

## 范围

本切片只实现 JSONL Bridge 启动时的协议版本区间与能力协商。它解决“双方都写着 version=1，
但无法证明功能是否兼容”的问题，为 HAR-08 协议评测、UI-17 发布门和 CC-02 Renderer 替换实验
提供稳定前置。

本切片不实现 ARC-03.1/2 的完整 schema registry，不实现 ARC-03.5 sequence gap recovery，
不生成 Python/TypeScript 类型，也不升级现有 envelope 主版本。

## 当前问题

- Python 和 Node 只接受与常量完全相等的 `version`；
- `hello` 只携带客户端名称，服务端 ACK 不声明版本区间或能力；
- UI 在收到启动 `ready` 后即视为可用，无法区分“进程存活”和“协议已兼容”；
- 不兼容只能得到通用 `bad_request`，不能给用户明确的升级方向；
- protocol contract 没有机器可读的协商字段，HAR-08 无法构造兼容性 fixture。

## 契约

`protocol-contract.json` 新增：

```json
{
  "negotiation": {
    "minimum_version": 1,
    "maximum_version": 1,
    "capabilities": ["heartbeat", "typed_ui_messages", "workbench_snapshot"],
    "required_capabilities": ["typed_ui_messages"]
  }
}
```

版本区间为闭区间正整数。能力名称为稳定、去重、按字典序排列的 snake_case 字符串。
`required_capabilities` 必须是 `capabilities` 的子集。

客户端 `hello.payload`：

```json
{
  "client": "naumi-terminal-ui",
  "minimum_version": 1,
  "maximum_version": 1,
  "capabilities": ["heartbeat", "typed_ui_messages", "workbench_snapshot"]
}
```

服务端成功 ACK：

```json
{
  "event": "hello",
  "negotiation": {
    "selected_version": 1,
    "server_minimum_version": 1,
    "server_maximum_version": 1,
    "capabilities": ["heartbeat", "typed_ui_messages", "workbench_snapshot"]
  }
}
```

`selected_version` 取双方区间交集中的最高版本；`capabilities` 是双方能力交集。服务端要求的能力
若缺失，协商失败。

## 兼容规则

- 旧客户端缺少协商字段时，以 envelope `version` 作为最小/最大版本，并视为声明服务端全部能力；
  该兼容仅保持一个发布周期，服务端 ACK 仍返回完整协商结果。
- `minimum_version > maximum_version`、布尔值冒充整数、空/超长/非法能力名必须在协议边界拒绝。
- 版本区间无交集返回 `protocol_version_unsupported`，说明客户端区间和服务端区间。
- 缺少服务端 required capability 返回 `protocol_capability_missing`，列出缺失能力。
- 协商失败不得发送成功 ACK 或后续 `runtime/status`，也不得改变 Engine、会话或权限状态。
- 非 `hello` 事件仍使用已存在的 envelope version 严格校验；本切片不把它放宽为隐式降级。

## 前端状态

`ready` 继续表示 Python 进程已完成启动；新增 `protocolNegotiated` 表示握手成功。Node 在 hello ACK
到达前不得发送用户提交、控制或心跳事件。握手失败时显示中文错误并保持不可提交状态。

测试 fixture 必须回 ACK；这样测试环境与真实 Bridge 使用同一启动语义，不再把 `ready` 冒充协商。

## 安全与可观测性

- 协商 payload 只允许版本、客户端标识和公开能力名，不允许任意嵌套字段进入状态；
- 错误文本不包含环境变量、配置路径或密钥；
- debug trace 记录标准化后的 hello 与协商 ACK，可供 HAR-08 静态/回放评测；
- 能力交集必须由 contract allowlist 计算，客户端不能注入未知 feature flag。

## 验收

1. Python 单元测试覆盖成功、旧客户端、无版本交集、缺 required capability、畸形范围与非法能力。
2. Node contract 测试证明 hello 从同一 contract 生成，并验证协商 ACK。
3. 真实 Node UI ↔ Python Bridge 子进程完成一次 hello，双方记录相同 selected version/capabilities。
4. 不兼容 fixture 不发送 runtime status，用户看到明确中文错误，进程可安全退出。
5. 定向 Ruff、Python 编译、Python/Node 小模块测试通过；不运行全量测试。

