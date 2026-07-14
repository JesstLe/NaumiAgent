# Ollama Chat 推理适配器设计

## 文档状态

- 日期：2026-07-14
- 状态：已批准，待实施
- 范围：provider catalog 中 `ollama` 的原生 `/api/chat` 推理、现有 `/api/tags` 发现到执行闭环
- 非目标：自动启动/安装 Ollama、自动 pull 模型、OpenAI compatibility 路由、模型量化管理、集群调度

## 1. 目标

让 provider catalog 中声明为 `ollama` 的本地或远端 Ollama 模型通过现有
`ModelRouter.call()` 与 `ModelRouter.stream()` 真实执行 `/api/chat`。实现必须保留
system/user/assistant/tool 历史、非流式和流式文本、原生工具调用、工具结果续轮、独立
thinking、finish reason 与 token usage。

现有 `ModelDiscoveryService` 已能读取 Ollama `/api/tags`，本切片把“能列出来”补成
“发现后立即能执行”。`/models`、REST、新 Terminal UI 和 Textual TUI 继续消费同一个
`ProviderModelListing`，不增加前端专属 Ollama 逻辑。

## 2. 当前事实

- `APIFormat.OLLAMA`、`/api/tags` envelope 解析、TTL、single-flight、stale-if-error、
  2 MiB 响应限制和 500 模型上限已经存在；
- `build_provider_transport()` 当前没有 `OLLAMA` dispatcher，模型可以发现但 Router 调用
  会在网络前返回“适配器尚未实现”；
- 当前锁定的 LiteLLM 同时提供 `ollama/` completion 与 `ollama_chat/` chat transport；
  `ollama_chat/` 使用 `/api/chat`，支持 OpenAI message schema、tools、thinking、流式 NDJSON、
  finish reason 和 usage；
- 本机安装了 Ollama 0.17.7 客户端，但当前 daemon 未运行，因此自动化验收使用真实本地
  HTTP loopback，不启动后台服务，不下载模型；
- Ollama 本地 endpoint 默认不需要认证；直连 `ollama.com` cloud API 时需要 Bearer key。

## 3. 方案比较

### 方案 A：LiteLLM 原生 `ollama_chat/`（采用）

catalog `ollama` 映射为 `ollama_chat/<upstream_model>`，显式传入 catalog 的 base URL、
认证 header 与 timeout。优势是保持原生协议身份并复用已安装依赖的 tools/thinking/stream
转换；风险集中在 LiteLLM 的认证回退和 NDJSON 契约，可通过 transport 单测与真实回环
锁定。

### 方案 B：Ollama OpenAI compatibility

用户可以另建 `openai_chat` provider 指向 Ollama `/v1`，现有代码无需新增适配器。但这会
让 `apiFormat: ollama` 与实际协议不一致，也无法证明 `/api/tags` 到原生 `/api/chat`、
thinking 和 Ollama tool result 的闭环，因此不作为本实现。

### 方案 C：Naumi 直接实现 Ollama HTTP/NDJSON

可以完全控制认证和错误，但必须重复维护 message/tool/thinking/stream/usage 的双向转换，
还会迫使 Router 增加供应商分叉。当前 LiteLLM 已覆盖这些协议细节，因此拒绝。

## 4. Catalog 契约

本地配置示例：

```json
{
  "providers": {
    "ollama": {
      "name": "Local Ollama",
      "apiFormat": "ollama",
      "baseURL": "http://127.0.0.1:11434",
      "requestTimeoutMs": 120000,
      "auth": {"type": "none"},
      "models": {
        "coding": {
          "upstreamId": "qwen3-coder:latest",
          "name": "Local Coding Model",
          "capabilities": {"tools": true}
        }
      },
      "discovery": {
        "enabled": true,
        "path": "/api/tags",
        "ttlSeconds": 300
      }
    }
  }
}
```

`baseURL` 必须是服务根地址，不能写成 `/api` 或 `/api/chat`：LiteLLM 在推理时追加
`/api/chat`，发现服务按 catalog 的 `discovery.path` 追加 `/api/tags`。协议由
`apiFormat` 决定，不按端口、host 或 provider ID 猜测。

Ollama 模型名保留 namespace、斜杠和 tag，例如 `org/model:tag`。它只进入 JSON body，
不参与 URL 拼接；仍受 catalog 的 256 字符、非空和控制字符边界约束。

## 5. Transport 与认证

新增：

```python
build_ollama_chat_transport(
    target: ResolvedModelTarget,
    *,
    catalog_source: str,
) -> ProviderTransport
```

映射：

- `model = f"ollama_chat/{target.upstream_model}"`；
- `api_base = provider.base_url.rstrip("/")`；
- `timeout = request_timeout_ms / 1000`（声明时）；
- `extra_headers` 是 catalog 静态 header 与显式自定义认证 header 的不可变副本；
- `auth.type=bearer` 且是标准 `Authorization: Bearer` 时把解析后的 secret 作为显式
  `api_key` 交给 LiteLLM；
- `auth.type=api_key_header` 或自定义 bearer header 时只发送该显式 header，不附加额外
  Authorization；
- `auth.type=none` 不发送认证 header。

LiteLLM 的 `ollama_chat` 会在 `api_key` 为空时读取全局 `OLLAMA_API_KEY`。catalog 已经明确
选择 provider/auth，因此 Naumi 不允许这种隐式回退：

- 标准 bearer 显式传 key；
- none 或自定义 header 时传 `api_key=None`；
- 若此时进程存在非空 `OLLAMA_API_KEY`，在网络前返回中文错误，提示用户删除该环境变量
  或在 catalog 中显式选择 bearer；
- 不修改进程环境，不使用跨协程临时清空环境变量，不发送占位 Authorization；
- 凭据错误、repr、事件与日志不得包含 secret。

Naumi 不设置 `litellm.ollama_key` 或 `litellm.api_key` 全局变量。回环测试会同时设置一个
未选中的 `OLLAMA_API_KEY`，证明 none/custom 模式在发网前被阻断；无 ambient key 时请求
不含 Authorization。

## 6. Router 与协议行为

`build_provider_transport()` 增加 `APIFormat.OLLAMA` dispatcher。Router 的
`call()`、`stream()`、reasoning effort、消息修复、工具参数聚合和 usage 逻辑保持共享，
不增加 `provider.id == "ollama"` 分支。

LiteLLM `ollama_chat` 的预期映射：

- system/user/assistant 继续是 `/api/chat.messages[]`；
- `max_tokens` 映射到 `options.num_predict`；
- `temperature` 等采样值进入 `options`；
- tools 保留 OpenAI function schema；
- Ollama response 的 `message.tool_calls[].function.arguments` object 归一化为 Router 的 JSON
  字符串；
- tool result 续轮沿用 Router 的完整/缺失配对修复；
- response `message.thinking` 映射为 `reasoning_content`；
- catalog 声明的 reasoning effort 经 LiteLLM 映射到 Ollama `think`，未声明能力时 UI 不
  猜测；
- NDJSON stream 中的 content、thinking、tool_calls 和最终 usage 进入现有 `StreamChunk`。

模型本身不支持 tools/thinking 时，供应商错误进入现有中文错误边界；Naumi 不按模型名
伪造能力，也不自动改用另一个模型。

## 7. 发现到执行

现有 `/api/tags` 解析继续接受 `model` 或 `name` 字段。动态模型进入 Router overlay 后，
`ollama/<id>` 与 active-provider 下的 `<id>` 都必须能解析为同一 catalog target，并通过
`ollama_chat/<id>` 执行。

静态 alias 优先保留能力、显示名与限制；发现结果按 upstream ID 去重。连接失败时继续
使用 stale 列表，未运行 daemon 时显示中文“连接失败”而不是堆栈或原始响应。

## 8. 真实回环验收

新增 `tests/integration/test_ollama_chat_loopback.py`，用 `ThreadingHTTPServer` 提供：

- `GET /api/tags`：一个可对话模型和一个静态重复模型；
- `POST /api/chat` 非流式文本：验证 system/user、`stream:false`、`num_predict`、usage；
- 非流式工具：返回原生 `tool_calls`；
- 工具结果续轮：验证 assistant tool call 与 role=tool 被送回并返回最终文本；
- 文本/thinking 流：发送 NDJSON chunks，验证增量与最终 usage；
- 流式工具：发送原生 tool call chunk，验证 Router 快照与最终 JSON 参数。

回环断言：

- 所有推理请求只到 `/api/chat`，发现只到 `/api/tags`；
- `auth:none` 时没有 Authorization，catalog/env secret 引用不进入 body；
- 动态发现 ID 立即可执行，identity 为 `provider=ollama`、`api_format=ollama`；
- text、tools、tool result、thinking、finish reason、usage、stream 均通过真实 LiteLLM；
- server 总能 shutdown/join，测试不依赖本机 Ollama daemon 或已下载模型。

只运行 provider runtime、discovery、targets、Router transport 和新 loopback 小模块，加
Ruff、`py_compile`、`git diff --check`；不跑 Python/Node 全量测试。

## 9. 用户体验与文档

- README 的可执行协议列表加入 Ollama Chat；
- `docs/15-model-provider-configuration.md` 增加本地 none-auth、远端 bearer、
  `/models ollama --refresh` 和 daemon 未运行时的排障；
- `/models`、REST、新 UI、TUI 自动复用共享 listing，现有 `ollama` 显示名保持不变；
- 不自动启动 daemon、不自动 pull 大模型、不在启动时扫描 localhost；只有用户刷新模型或
  实际调用时才连接；
- 连接失败、认证冲突、ambient key 冲突、无效 base URL 都给中文原因与下一步。

## 10. 性能、并发与安全边界

- 发现保持 at-most-four provider 并发与 per-provider single-flight；
- 推理不持有全局环境锁、不修改 `os.environ`，不同 provider 可并发；
- timeout 显式从 catalog 进入 discovery 与 inference；
- response 继续受 LiteLLM/httpx 流式消费，不把完整 NDJSON 累积到额外缓冲；
- model ID 只进 JSON body，base URL 和 discovery path 由严格 catalog parser 约束；
- 静态 header 与认证 header 大小写冲突在 secret lookup 前失败；
- 原始服务错误正文和 secret 不进入用户文案。

## 11. 边界与后续顺序

本切片不实现：

- 自动安装/启动/健康守护 Ollama；
- `ollama pull`、模型删除、量化、Modelfile 或 GPU/线程自动调优；
- 多 Ollama 节点负载均衡；
- Ollama cloud 模型 catalog 内置默认值；
- OpenAI compatibility 的重复适配；
- 多模态 images、embeddings、generate、create/show/copy/push/pull 管理 API。

完成后继续 Azure OpenAI transport，再做 built-in provider registry 与 UI 配置向导。

## 12. 自审

- 无 TBD/TODO；原生 chat、发现、认证、动态执行、tools、thinking、stream、usage 均有
  明确 owner 和真实回环证据；
- 没有把“客户端已安装”当成 daemon 可用，也不启动用户后台服务；
- 没有用全局环境变量临时清空来换取 none-auth，避免并发竞态；
- 没有把 Ollama OpenAI compatibility 冒充原生 `ollama`；
- 没有硬编码“最新本地模型”，`/api/tags` 是当前 endpoint 的事实来源；
- 没有将自动 pull、GPU 调优、Azure 或 UI picker 混入同一实现切片。
