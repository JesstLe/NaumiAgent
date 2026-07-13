# OpenAI-Compatible Chat Adapter Design

## Goal

让 `ProviderCatalog` 中 `apiFormat=openai_chat` 的模型真正通过 LiteLLM 的
OpenAI-compatible Chat 路径执行，同时保持旧 `ModelConfig` 调用完全兼容。
本功能只实现 Chat Completions；OpenAI Responses、Anthropic、Google、Azure、
Ollama 和模型发现继续作为独立功能推进。

## Runtime boundary

新增独立的 provider runtime 模块，负责把一个已解析的
`ResolvedModelTarget` 转成 LiteLLM transport model 与安全 kwargs。Router 的
`call()` 和 `stream()` 共用同一个转换入口，避免两条链路漂移。

- legacy target：继续使用当前模型名和 `_base_kwargs()`。
- catalog + `openai_chat`：模型变为 `openai/<upstream_id>`，使用 provider 的
  `base_url`、headers、request timeout 和明确凭据。
- catalog + 其他 API format：返回中文“适配器尚未实现”，不得误走 Chat。
- catalog provider 缺少 `api_format` 或 `base_url`：返回中文配置错误，不按名称或
  URL 猜测。

Router 对外仍报告用户请求的 model alias；计费和 token 上限仍按 catalog target
解析。transport model 只用于底层请求。

## Credential resolution

凭据只在一次实际调用选中 provider 后解析，绝不在加载 catalog 或启动 Engine 时
枚举读取。

- `credential`：调用 `load_model_api_key(provider=secret_ref,
  fallback_to_legacy=False)`。目录已明确命名 provider 时禁止回退旧全局 Key。
- `env`：只读取 `secret_ref` 指定的精确环境变量；不扫描厂商常见变量。
- `file`：绝对路径按显式路径读取；相对路径锚定 catalog JSON 所在目录，解析后
  必须仍位于该目录内。内存 catalog 的相对路径被拒绝。
- `none`：不读取任何凭据。

secret 文件只接受普通 UTF-8 文件，最大 64 KiB；首尾空白被移除，空值被拒绝。
错误消息只包含 provider、来源类型和安全路径，不包含 secret 内容或底层异常。

## Authentication mapping

- 标准 `Authorization: Bearer` 使用 LiteLLM `api_key`。
- 自定义 bearer header 和 `api_key_header` 放入 `extra_headers`，不把 secret 同时
  复制到 `Authorization`。
- `none` 与自定义认证头使用固定非机密占位 API key，阻止 LiteLLM 从
  `OPENAI_API_KEY` 等全局环境变量回退；占位值不得来自用户环境。
- provider 静态 headers 与认证 header 合并；解析器已禁止静态 headers 覆盖敏感
  认证头，运行时仍检查冲突。

该占位 key 可能形成一个无敏感信息的 dummy Authorization header，这是当前
LiteLLM/OpenAI SDK 要求显式 key 且又必须阻止环境变量串用时的可控折中；真实 secret
不会被复制到错误 header。

## Request options

- `base_url` → LiteLLM `api_base`。
- `request_timeout_ms` → 秒制 `timeout`，允许小数。
- 静态 headers 与认证 header → 新 dict 的 `extra_headers`。
- `chunk_timeout_ms` 不传给 LiteLLM。本功能不假装支持；后续在 Naumi 的 stream
  消费边界独立实现逐 chunk 超时。

## Errors and compatibility

统一抛出 `ProviderRuntimeError`，用户文案为中文。缺 Key、文件越界、文件过大、
unsupported format、missing base URL 都在调用 LiteLLM 前失败。

没有 catalog、legacy target、旧 `api_base/api_key`、Kimi thinking、消息清理、工具
调用聚合、usage 和 cost 行为保持不变。

## Verification

1. 纯 runtime 单测覆盖 bearer、custom header、env/file/credential/none、相对路径
   confinement、缺失/空/过大 secret、timeout、headers、unsupported format。
2. Router call/stream 定向测试证明二者使用同一 mapping，且 legacy kwargs 不变。
3. 用本地临时 HTTP server + 非机密测试 token 跑一次真实 LiteLLM Chat 请求，检查
   URL、model、headers 和 response 闭环；不访问外网或真实 Key。
4. 只运行 provider runtime、Router、catalog/target 小模块测试和相关 Ruff/import。

## Deferred work

1. OpenAI Responses adapter (`openai/responses/<upstream>`).
2. Naumi stream chunk timeout。
3. Anthropic Messages、Google GenAI、Azure OpenAI、Ollama adapters。
4. 远程模型发现、UI/TUI provider picker。
