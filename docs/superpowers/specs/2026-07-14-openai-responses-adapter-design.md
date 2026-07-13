# OpenAI Responses Adapter Design

## Goal

让 `ProviderCatalog` 中 `apiFormat=openai_responses` 的模型通过 LiteLLM 的
Responses bridge 执行，并继续复用 Naumi 现有 `ModelResponse` / `StreamChunk`、工具
调用、usage 和计费接口。

本功能只增加 Responses transport 选择，不实现 Anthropic、Google、Azure、Ollama，
也不改变已经上线的 OpenAI-compatible Chat 与 legacy 路径。

## Transport mapping

provider runtime 增加统一 dispatcher：

- `openai_chat` → `openai/<upstream_id>`。
- `openai_responses` → `openai/responses/<upstream_id>`。
- 其他格式 → 中文“适配器尚未实现”。
- 缺少 `api_format` / `base_url` → 中文配置错误。

Chat 与 Responses 共用已验证的认证、base URL、静态 headers、request timeout、
secret 文件 confinement 和防全局 Key 回退逻辑，不复制第二套 credential resolver。

Router 只调用统一 dispatcher。LiteLLM 的 `acompletion()` 在看到
`openai/responses/` 前缀后，把 Chat messages/tools 参数转换为 `/responses` 请求，
再把非流式和流式结果转换回 Chat-compatible 对象，因此 Naumi 现有响应解析保持
单一实现。

## Native streaming capability

LiteLLM 1.85.0 会把注册表中未知的 Responses model 判定为“不支持原生流式”，进而
先请求完整 JSON，再生成 fake stream。catalog 的自定义 upstream 往往不在 LiteLLM
内置表中，因此 Router 在第一次流式调用前，通过 LiteLLM 的公开 `register_model()`
为该 `openai/<upstream>` 声明 `mode=responses` 和
`supports_native_streaming=true`。

- 只对 `openai_responses` 生效；Chat/legacy 不注册。
- 每个 Router + upstream 只注册一次。
- 注册由短临界区保护，并发首次调用不会重复写注册表。
- 注册失败在网络请求前显式失败，不静默退回 fake stream。
- provider 若谎报 Responses 协议但实际不支持 SSE，由真实上游错误暴露，系统不伪装
  成流式成功。

## Compatibility and public identity

- 底层 transport model 使用 Responses 前缀。
- `ModelResponse.model`、metadata lookup、cost 和 tier 仍使用用户请求 alias。
- Kimi thinking 协议判断仍基于 public/upstream identity 和 provider base URL。
- legacy 与 `openai_chat` kwargs 必须逐项保持现状。
- Responses provider 不再报“尚未实现”；未支持的其他格式仍在 LiteLLM 前失败。

## Authentication and errors

Responses 与 Chat 使用同一 `ProviderTransport`：

- 标准 Bearer 通过显式 `api_key`。
- custom header / no-auth 使用固定非机密 placeholder，禁止继承
  `OPENAI_API_KEY`。
- selected provider 的 credential/env/file 规则、错误脱敏、64 KiB 限制和控制字符
  拒绝全部不变。

不捕获或改写 LiteLLM 的 HTTP/API 错误；本层只负责在传输前给出 provider 配置错误。

## Verification

1. provider runtime RED/GREEN：Responses 前缀、共享 kwargs、dispatcher、unsupported
   format、Chat 回归。
2. Router call/stream RED/GREEN：两条链路都选 Responses transport，public model 不变；
   Chat 与 legacy 回归；并发首次 stream 只注册一次原生流式能力。
3. 本地临时 `/v1/responses` HTTP server + 非机密测试 token，真实执行 LiteLLM
   Responses bridge，验证请求 input/model/header 与 Naumi `ModelResponse`。
4. 只运行 runtime/router/catalog/target 与 Engine catalog 小模块测试、Ruff 和编译。

## Deferred work

1. Responses 原生能力扩展：`previous_response_id`、background、store、reasoning summary。
2. stream chunk timeout。
3. Anthropic/Google/Azure/Ollama adapters。
4. provider/model picker 同步到新 UI 与 TUI。
