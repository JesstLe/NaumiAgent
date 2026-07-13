# Provider 运行身份 UI 设计

## 目标

让新终端 UI 与 Textual TUI 使用同一份后端事实，明确展示当前模型别名、provider、API 协议与真实上游模型映射。界面不得根据模型名或 URL 猜测 provider，也不得读取或输出凭据。

## 数据来源

`ModelRouter` 在现有 `resolve_target()` 之上提供不可变的运行身份：

- `requested_model`：用户配置的模型名或别名；
- `canonical_model`：catalog 中的规范模型名；
- `upstream_model`：实际发给 provider 的模型 ID；
- `provider`：catalog provider ID；legacy 路径使用配置中的 provider，未配置时为空；
- `api_format`：catalog 声明的 API 协议；legacy 路径标记为 `legacy`；
- `source`：`catalog` 或 `legacy`。

该方法只解析内存中的配置与 catalog，不访问网络、凭据或文件。

## 展示规则

- Bridge 的 `ready` 与 `runtime/status` 增加 `provider`、`api_format`、`upstream_model`。
- 新 UI 欢迎页展示 provider/协议；仅当上游模型与当前模型不同时展示映射。
- 新 UI 底部状态栏增加紧凑的“提供方/协议”字段，并沿用现有自动换行，不能省略其他状态项。
- Textual TUI 启动状态栏展示同一 provider/协议与可选映射。
- `openai_chat`、`openai_responses`、`legacy` 使用中文可读标签；未知值原样显示，避免伪造事实。

## 边界与错误处理

- catalog provider 缺少 `apiFormat` 时返回空协议，由界面显示“未解析”。
- 运行身份解析失败时，状态接口仍可用，新增字段为空，不影响会话启动。
- 所有协议字段在 JSONL 边界执行严格字符串校验，拒绝对象注入。
- 本切片不实现 provider 选择器、模型发现或新的协议适配器。

## 验证

- Router：catalog、legacy、缺少协议三条路径。
- Bridge：权威状态字段与失败降级。
- 新 UI：协议归一化、欢迎页、底部状态栏及窄屏边界。
- TUI：真实临时 catalog 启动，验证 provider、协议与模型映射可见。
