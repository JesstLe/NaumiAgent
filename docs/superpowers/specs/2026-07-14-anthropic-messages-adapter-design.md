# Anthropic Messages 适配器设计

## 目标

让 provider catalog 中声明为 `anthropic_messages` 的模型通过 `ModelRouter.call()` 与 `ModelRouter.stream()` 真实调用 Anthropic Messages 协议，并继续输出 Naumi 统一的文本、工具调用、流式增量与 usage 结构。

## 传输映射

- catalog 模型映射为 LiteLLM 的 `anthropic/<upstream_model>`。
- `baseURL`、静态 headers、请求超时与 provider 范围凭据沿用现有安全解析链路。
- Anthropic 标准 `x-api-key` 认证把选中的 secret 作为显式 `api_key` 交给 LiteLLM，避免其生成的认证头覆盖 catalog header。
- OpenCode `@ai-sdk/anthropic` 的 `options.apiKey` 必须解析为 `X-API-Key`，不能沿用 OpenAI-compatible provider 的 bearer 默认值。
- bearer 或自定义认证头保留为显式 header，同时传入固定的非机密占位 key，阻止回退读取全局 `ANTHROPIC_API_KEY`。
- `auth.type=none` 同样使用固定占位 key，确保不会意外读取机器全局密钥。

## Router 行为

- 非流式与流式调用共用同一 transport。
- 消息与工具 schema 继续使用 OpenAI 统一形态，由 LiteLLM 做 Anthropic Messages 的 content block 转换；返回结果继续转换为 `ModelResponse` / `StreamChunk`。
- Router 向 LiteLLM 保留 `stream_options.include_usage`，LiteLLM 用它生成最终 usage chunk；真实 loopback 必须同时证明该控制参数不会进入 Anthropic `/v1/messages` 请求体。
- Responses 专属的原生流注册只对 `openai_responses` 生效，不触碰 Anthropic。

## 边界

- 缺少 `baseURL`、协议错配、凭据缺失、header 冲突均在发网前返回中文错误。
- 不从模型名或 URL 猜测协议；只信 catalog 的 `apiFormat`。
- 本切片不实现 Anthropic Files、Batch、原生 MCP 或 computer-use beta；这些能力后续独立推进。

## 验证

- transport 单测覆盖 none、标准 x-api-key、bearer、自定义 header、协议错配与 dispatcher。
- Router 单测覆盖非流式、流式、工具调用与不支持协议的发网前失败。
- 本地 loopback 真实运行 LiteLLM，验证 `/v1/messages` 请求体、认证头、文本响应、工具调用与 SSE 增量。
