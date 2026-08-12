# ARC-01.3d1 Structured Tool Failure Contract

## 目标

为已经授权的 `Tool.execute()` 增加可选、稳定且可安全展示的声明式失败契约，使本地 Engine、Agent Worker、
New UI/TUI 事件和后续 EVO Sandbox 能机械区分错误类型与可重试性。本切片是
[EVO-06.3b2](../self-evolution/EVO-06-3b1-executable-scenario-binding.md) 执行错误 oracle 的最小运行时前置，
不增加新 Port、不改变权限语义、不授予候选代码执行或 Registry authority。

## 契约

工具在预期领域失败时可以抛出：

```python
raise ToolExecutionError(
    "source_unavailable",
    "来源暂时不可用，请稍后重试。",
    retryable=True,
)
```

- `code` 固定为 1..64 字符的小写标识符；
- `message` 为 1..300 字符的用户安全单行文本，拒绝控制字符和疑似凭据；
- `retryable` 必须为布尔值；
- 未迁移的 Tool 仍可返回字符串或抛出普通异常，兼容行为不变；
- `ToolResult` 只在 `status=error` 时允许携带 `error_code/retryable`；
- Engine 对声明式失败不暴露 Python 异常类名，只投影安全消息、错误码和可重试性；
- Engine 在投影前重新构造并校验异常，候选代码创建后篡改字段或 `args` 会固定降级为
  `tool_failure_contract_invalid`，原始内容不进入事件和 UI；
- `tool_end` 事件、typed UI message 与 Agent Worker Tool RPC 保留相同字段；
- New UI 的工具卡保留错误码，可重试失败使用黄色并显示“可重试”；fallback TUI/CLI 同步显示错误码与重试提示。

## RPC 兼容

Agent Worker result batch 保持 schema version 1 的 additive compatibility：默认空字段不编码，旧 payload 的摘要
和解码继续有效；出现声明式失败时才加入 `error_code` 与 `retryable=true`，两字段参与批次摘要和读取校验。
成功、skipped 或 aborted 结果携带结构化失败字段会 fail closed。

## 安全与边界

- 该异常只表达 Tool 已声明、可向用户公开的领域失败，不代替日志中的内部异常；
- Sandbox 候选只能用 Specification 已声明的 code 通过 error oracle，不能靠异常类名或消息模糊匹配；
- ordinary exception 暂时仍走既有兼容投影，统一内部错误脱敏属于独立安全切片；
- bypass 只跳过权限确认，不放宽错误码、消息、RPC 或结果不变量；
- 本切片不 materialize/import 候选 Artifact，不运行场景，也不签发 Registry lease。

## 验收标准

- 有效声明式失败从 Port 传播到 `ToolResult`，code、message、retryable 逐字段保持；
- 非法 code、secret-like/control message、非布尔 retryable 与矛盾 `ToolResult` 被拒绝；
- UI typed message 保留结构化字段；
- New UI 与 fallback TUI/CLI 对结构化失败提供有区别的可见反馈；
- Agent Worker 新结构化结果 round-trip，旧无字段 payload 仍可 round-trip；
- 普通异常与取消语义不回归；
- 只运行相关 Ruff、编译和小模块 pytest，不用全量测试冒充本切片证据。

## 自我审视与下一步

本契约解决了“错误 oracle 无法机械判断”的真实缺口，但尚未验证任一候选工具。下一步必须回到 EVO/Harness：
建立 content-addressed Sandbox Execution Request，把 sealed Artifact、Scenario Binding、exact source revision、
permission profile 与 ARC-04 Run Grant 绑定，再由隔离 Worker 逐场景执行并签发可撤权 Receipt。
