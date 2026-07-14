# Google GenAI 适配器设计

## 文档状态

- 日期：2026-07-14
- 状态：已批准并实现
- 范围：provider catalog 中 `google_genai` 的原生推理 transport、Google 模型发现与既有 Router/UI 列表闭环
- 非目标：Vertex AI、Google Interactions API、OpenAI-compatible Gemini 路由、内置厂商 catalog、真实付费外网测试

## 1. 目标

让 provider catalog 中声明为 `google_genai` 的 Google AI Studio / Gemini 模型通过
`ModelRouter.call()` 与 `ModelRouter.stream()` 真实调用 Gemini 原生
`generateContent` / `streamGenerateContent` 协议，并继续输出 Naumi 统一的文本、
工具调用、思考内容、流式增量与 usage 结构。

同一 provider 的 `discovery.enabled=true` 必须能够调用 Google `GET /models`，只把
支持 `generateContent` 的模型加入现有 `/models`、REST、新 Terminal UI 和 TUI 共用
列表。这样“最新几个模型”来自厂商实时模型端点和 TTL 缓存，而不是写死在源码中后
快速过期。

## 2. 当前事实

现有多厂商基础设施已经完成：

- provider-scoped 系统凭据、环境变量与受限文件 secret 引用；
- 严格 JSON catalog、OpenCode provider shape、模型别名和 tier 解析；
- OpenAI Chat、OpenAI Responses、Anthropic Messages transport；
- OpenAI/Ollama 模型发现、TTL、single-flight、stale-if-error 与 2 MiB 响应上限；
- `/models`、REST、新 Terminal UI、TUI 的共享模型列表；
- 静态模型能力与运行时思考强度。

本设计实施后，`APIFormat.GOOGLE_GENAI` 已接入 `build_provider_transport()` 和
`ModelDiscoveryService`。catalog 的 OpenCode `@ai-sdk/google` secret 会归一化为
`api_key_header` + `X-Goog-Api-Key`；原生 catalog 继续支持 provider-scoped 系统凭据、
环境变量和受限文件引用。

当前固定 LiteLLM 版本的 `gemini/` 原生 provider 负责消息、工具、流式响应、usage 和
Gemini 思考参数转换。Naumi 显式提供 `api_base`、认证和模型能力声明，并通过本地真实
HTTP 回环锁定两者边界，没有复制一套平行 Gemini 客户端。

## 3. 方案比较

### 方案 A：LiteLLM 原生 `gemini/` transport（采用）

catalog `google_genai` 映射为 `gemini/<upstream_model>`，凭据和 endpoint 仍由 Naumi
安全层显式解析。优点是保持真实协议身份，又复用成熟的消息、工具、流式、思考和
usage 转换。风险集中在 LiteLLM 参数契约，可由 transport 单测和本地 HTTP loopback
锁定。

### 方案 B：Gemini OpenAI compatibility

把 Gemini endpoint 当作 `openai_chat` 可以较快调用，但会让 catalog 声明的
`google_genai` 与实际 transport 不一致，也无法验证原生 Google 工具和 usage 行为。
用户若明确选择 Google 的 OpenAI compatibility，可另建 `openai_chat` provider；它
不是本适配器的实现捷径。

### 方案 C：Naumi 直接实现 Gemini HTTP/SSE

控制能力最强，但需要自行维护 system instruction、Content/Part、functionCall、
functionResponse、thought signature、SSE、finish reason 与 usage 的双向转换，重复当前
依赖并增加跨模型回归面，因此拒绝。

## 4. Catalog 与配置契约

原生配置示例：

```json
{
  "providers": {
    "google": {
      "name": "Google AI Studio",
      "apiFormat": "google_genai",
      "baseURL": "https://generativelanguage.googleapis.com/v1beta",
      "requestTimeoutMs": 60000,
      "auth": {
        "type": "api_key_header",
        "env": "GEMINI_API_KEY",
        "header": "X-Goog-Api-Key"
      },
      "models": {
        "gemini-fast": {
          "upstreamId": "gemini-3.5-flash",
          "name": "Gemini Flash",
          "capabilities": {
            "tools": true,
            "reasoning": ["minimal", "low", "medium", "high"]
          }
        }
      },
      "discovery": {
        "enabled": true,
        "path": "/models",
        "ttlSeconds": 3600
      }
    }
  }
}
```

`baseURL` 必须包含 API 版本前缀，因为 LiteLLM 对自定义 Gemini base 拼接
`/models/<model>:generateContent`。本切片不按 provider 名或 URL 猜版本，也不为缺失
endpoint 静默填默认值。

OpenCode `npm: "@ai-sdk/google"` 且存在 secret reference 时，catalog loader 将认证
归一化为 `api_key_header` + `X-Goog-Api-Key`。明文 key 继续被拒绝。OpenCode 配置若
没有 `baseURL`，对象可以被安全解析和展示，但运行与发现会在发网前返回缺少
`baseURL`；内置厂商默认 endpoint 属于后续 built-in provider registry。

## 5. Transport 映射

新增：

```python
build_google_genai_transport(
    target: ResolvedModelTarget,
    *,
    catalog_source: str,
) -> ProviderTransport
```

映射规则：

- model：`gemini/<upstream_model>`；若 upstream 已带 `models/`，规范化时只移除一个
  官方资源名前缀，禁止路径、查询或 endpoint 片段；
- `api_base`：catalog `baseURL.rstrip("/")`；
- `api_key`：标准 `X-Goog-Api-Key` secret 作为显式 key 交给 LiteLLM，由其生成官方
  header；
- `extra_headers`：只包含 catalog 静态非认证 headers；
- `timeout`：复用 `request_timeout_ms / 1000`；
- `chunk_timeout_ms` 仍由 Router 的现有流式超时边界处理，不在 transport 复制。

对于自定义认证：

- 标准 `api_key_header` 且 header 大小写等价于 `x-goog-api-key`：传真实 `api_key`，
  不重复放进 `extra_headers`；
- bearer 或其他自定义 API-key header：真实 secret 只进入显式 header，同时传固定
  非机密占位 key，阻止 LiteLLM 回退读取机器上的 `GEMINI_API_KEY` / `GOOGLE_API_KEY`；
- `auth.type=none`：同样传固定占位 key，支持明确配置的本地兼容代理且不读取全局
  secret；
- 静态 header 与认证 header 冲突：在 secret lookup 和网络之前失败；
- 错误、repr、事件和日志不得包含 secret。

`build_provider_transport()` 增加 `GOOGLE_GENAI` dispatcher 分支。Router 的
`call()`、`stream()` 和动态发现模型执行继续走一个 transport 构造入口，不创建
Google 专属 Router 分叉。

## 6. Router 行为

Naumi 继续向 LiteLLM 提交统一 OpenAI message/tool schema：

- 非流式返回映射为 `ModelResponse.content/tool_calls/reasoning_content/usage`；
- 流式返回映射为现有 `StreamChunk`，工具参数增量继续由 Router 聚合；
- tool result 继续使用当前消息修复与配对逻辑，不绕过缺失 tool result 防护；
- catalog 声明的 reasoning efforts 继续通过现有 `reasoning_effort` 选择进入
  LiteLLM；没有能力声明时 UI 不伪造思考强度；
- Google transport 声明 `supports_system_messages` 能力，Router 通过通用、线程安全且
  幂等的 transport registration 入口注册；这让动态发现而尚未进入 LiteLLM 静态表的
  模型仍保留原生 `systemInstruction`，而不是把 system 文本拼进 user parts；
- Responses 原生流注册继续是独立的 stream-only 能力，不与 Google 注册逻辑混用。

任何 protocol mismatch、缺少 endpoint、无效 upstream model 或凭据错误都必须在
网络前给出稳定中文错误。模型返回安全拦截或空 candidates 时由 LiteLLM 错误归一化
进入现有 ModelRouter 用户错误路径，原始响应正文不直接展示。

## 7. Google 模型发现

`ModelDiscoveryService` 增加 `GOOGLE_GENAI`：

- 请求 URL：`baseURL.rstrip("/") + discovery.path`；示例为
  `https://generativelanguage.googleapis.com/v1beta/models`；
- 认证沿用 `build_provider_http_config()`，标准 key 使用 `x-goog-api-key`；
- 仍使用 2 MiB、500 项、256 字符 ID、控制字符、HTTP 状态、timeout、single-flight、
  TTL、失败负缓存和 stale-if-error 等已有边界；
- envelope 必须是 object 且 `models` 为数组；
- `name` 必须是 `models/<id>`，公开 ID 移除一个 `models/` 前缀；
- `supportedGenerationMethods` 存在时必须包含 `generateContent`，否则作为不可用于
  Agent 对话的记录忽略并形成计数告警；
- 重复、无效、截断行为继续复用 `_ParsedModels`；
- 静态模型元数据仍优先，远程模型只补实时 ID，不猜 context、价格、工具或思考能力。

发现结果进入现有 Router dynamic overlay，因此 `/models google`、REST config、新 UI
和 TUI 自动看到同一列表；发现出来的模型也能被 `google/<id>` 实际调用，不是只读
装饰数据。

## 8. 端到端验证

### 8.1 Catalog / transport 单测

- OpenCode `@ai-sdk/google` secret 映射为 `X-Goog-Api-Key`；
- 标准 key、自定义 header、bearer、none、静态 header 冲突和缺失 baseURL；
- model prefix、官方 `models/` 前缀、timeout、dispatcher 和 protocol mismatch；
- `OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`GOOGLE_API_KEY`、`GEMINI_API_KEY` 的残留值
  不能覆盖 catalog 明确选择。

### 8.2 Discovery 单测

- 真实 Google models envelope、`models/` 规范化、generateContent 过滤；
- 空/畸形/重复/超限/控制字符记录与无可用模型；
- 认证头、URL、HTTP 错误、TTL、stale、single-flight 与 waiter cancellation；
- 静态模型和远程 upstream 去重。

### 8.3 Router 与本地 loopback

使用本地 HTTP server 和非真实 key，真实经过 LiteLLM：

- 非流式 `:generateContent`：验证 URL、`x-goog-api-key`、system/user/tool schema、文本、
  functionCall 和 usage；
- 流式 `:streamGenerateContent`：验证 SSE/JSON chunk、文本增量、工具参数、finish reason
  和最终 usage；
- 完成一轮 Agent tool request → tool result → final answer，证明消息回灌真实可用；
- `/models` loopback 返回实时模型并能立即路由其中一个动态模型；
- 请求体和日志中不出现 discovery-only 字段、catalog secret reference 或全局 key。

验证只运行 provider catalog/runtime/discovery/router 和新 loopback 文件，外加 Ruff；不
运行 Python 全量测试，不访问 Google 外网，不访问真实 Keychain。

### 8.4 实施证据

- 最终聚焦验收覆盖 provider catalog、runtime、discovery、targets、Router、Router
  transport 和 Google loopback 七个文件：分支上 `225 passed in 3.46s`，快进合并后的
  `main` 再验为 `225 passed in 5.20s`；
- Ruff 对上述生产文件与测试文件通过，四个生产模块 `py_compile` 通过，
  `git diff --check` 通过；
- 本地 Gemini 回环真实经过已安装 LiteLLM，覆盖 `/models`、动态模型立即执行、
  `systemInstruction`、非流式文本、非流式工具、工具结果续轮、文本流、流式
  functionCall、finish reason、usage 和 `X-Goog-Api-Key`；
- 临时 `.naumi/config.yaml` + `providers.json` smoke 验证了相对 catalog 路径锚定、
  `/models` 过滤、运行身份和一次 `:generateContent`，退出后临时目录已清理；
- 验收没有访问 Google 外网、没有读取或修改真实 Keychain、没有触碰正式 `.naumi`，
  也没有运行 Python 或 Node 全量测试。

## 9. 用户体验

- `/models google` 显示静态与发现模型、缓存状态和安全告警；新 UI/TUI 无需另写
  Google 列表逻辑；
- 运行状态继续显示 `provider=google`、`api_format=google_genai`、canonical/upstream
  model，让用户能分辨原生 Gemini 与 OpenAI compatibility；
- 缺 key、缺 baseURL、认证失败、发现失败、协议错配都给中文原因和下一步；
- 暂时失败且有旧列表时标记 stale，不把整个模型选择界面变成错误页；
- 不显示或记录 API key，不在启动时预取所有 provider，避免再次触发 macOS Keychain
  提示。

## 10. 边界与后续顺序

本切片不实现：

- Google Interactions API。官方已推荐它用于新 agentic 工作流，但当前 Naumi 的统一
  Router/LiteLLM chat contract 先通过仍受支持的原生 generateContent 完成闭环；
  Interactions 的 typed step、server-side state 与 background 需要独立协议设计；
- Vertex AI OAuth/service account、project/location routing；
- Gemini OpenAI compatibility；用户可显式声明单独的 `openai_chat` provider；
- Google Files、Live、Batch、Embedding、Imagen、Veo 与 server-side built-in tools；
- models.dev 价格/上下文元数据；
- built-in provider 默认 endpoint 与 UI 中的 provider 配置向导。

完成后依次推进 Ollama inference、Azure OpenAI、built-in provider catalog/UI picker，再
评估 Google Interactions API。

## 11. 自审

- 没有按模型名或 URL 猜协议；catalog `apiFormat` 仍是唯一 transport 事实。
- 没有把 Google 原生协议伪装成 OpenAI Chat。
- 没有硬编码会过期的“最新模型”；模型端点和 TTL 是动态事实来源。
- 没有在启动时枚举 provider 或访问 Keychain。
- 标准和自定义认证都阻止 LiteLLM ambient environment fallback。
- 发现与推理复用同一 provider/security identity，但各自保留正确 header 语义。
- 动态模型所需的 LiteLLM 能力由 transport 声明，Router 只执行通用注册，不按 provider
  ID 分叉；注册失败在网络前脱敏，单 Router 并发只注册一次。
- 设计覆盖 call、stream、tools、usage、reasoning、discovery、Router 动态模型和三端
  展示，不以“transport 对象生成成功”冒充端到端可用。
- Vertex、Interactions、Azure、Ollama 没有混入同一提交。
