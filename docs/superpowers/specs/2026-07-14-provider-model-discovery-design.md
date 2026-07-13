# Provider Model Discovery Design

## Goal

让 NaumiAgent 从 provider catalog 声明的真实端点自动获取可用模型，并让发现结果可以被 Router、REST、新终端 UI 和 TUI 共同使用。首个切片覆盖 OpenAI-compatible `GET /models` 与 Ollama `GET /api/tags`，同时保留静态模型作为稳定回退。

## Current Evidence

`ModelDiscoverySpec`、`discovery.path`、TTL、白名单和黑名单已经能够从 JSON catalog 严格解析，但当前没有任何代码执行发现请求。`ModelRouter` 只能解析静态 `ProviderSpec.models`；REST `/config` 仍硬编码 `kimi-for-coding`；`/model` 只能显示三档配置，不能列出可用模型。

OpenAI 官方接口以 `GET /v1/models` 返回 `data[].id`。Ollama 官方接口以 `GET /api/tags` 返回 `models[].model` 与 `models[].name`。OpenCode 的 `models` 命令把模型统一显示为 `provider/model` 并支持刷新；其最新模型元数据主要来自 models.dev。Naumi 本切片先实现 provider 端点发现，解决自建网关、OpenAI-compatible 厂商与本地 Ollama 的实时列表问题；models.dev 元数据补全作为后续独立能力。

## Alternatives

### Eager startup discovery

启动时并发拉取全部 provider。列表最早可用，但会拖慢欢迎界面、提前访问 Keychain，并在多 provider 配置中产生无关的系统授权提示。拒绝。

### Lazy discovery with TTL and single-flight

只有 `/models`、REST 配置页、模型选择器或一个静态 catalog 未声明的模型首次被调用时才发现当前 provider。每个 provider 使用 TTL 缓存；并发请求共享同一个 in-flight task。选择该方案。

### Periodic background refresh

常驻定时任务可以保持列表最新，但引入重试风暴、进程生命周期和跨平台后台调度问题。等用户界面模型选择器稳定后再评估。

## Architecture

### Discovery service

新增 `src/naumi_agent/model/discovery.py`：

- `AvailableModel`：公开模型 ID、canonical ID、upstream ID、名称、来源以及静态能力元数据。
- `ProviderModelListing`：provider ID、模型元组、缓存状态、是否 stale、刷新时间与安全告警。
- `ModelDiscoveryService`：接受不可变 catalog、catalog 来源与可注入 HTTP client/单调时钟；负责请求、解析、合并、缓存和并发去重。

服务不会扫描 catalog 中的所有凭据。`list_provider(provider_id)` 只解析该 provider；`list_all()` 最多四路并发，且静态-only provider 不访问网络或凭据。

### Secure HTTP configuration

`provider_runtime.py` 新增公开的 `ProviderHTTPConfig` 与 `build_provider_http_config(provider, catalog_source)`。它复用现有 secret resolver、敏感 header 冲突检查和 provider-scoped credential 规则，把认证归一化为一次 HTTP 请求所需的 headers、base URL 与 timeout。

标准 bearer 转成 `Authorization: Bearer ...`；自定义 bearer/API-key header 保持 catalog 声明；`auth=none` 不读取全局环境 Key。对象 `repr` 不包含 headers，错误不包含 secret、响应正文或带查询的 URL。

### Bounded endpoint clients

发现 URL 由 `provider.base_url.rstrip("/") + "/" + discovery.path.lstrip("/")` 构造，保留 `/v1` 等 base path。只接受 catalog 已验证的 HTTP(S) URL 和相对 path。

- `openai_chat`、`openai_responses`：解析 object 根节点中的 `data` 数组和非空 `id`。
- `ollama`：解析 object 根节点中的 `models` 数组，模型 ID 优先 `model`，其次 `name`。
- 其他 API format 明确返回“发现适配器尚未实现”，不按 URL 或 provider 名称猜测。

使用 httpx streaming response，累计正文超过 2 MiB 立即中止；最多接受 500 项；单个模型 ID 最长 256 字符且不能含控制字符。重复 ID 去重，无效条目计入安全告警；非空数组若没有任何有效模型则失败。

HTTP 401/403、404、429、超时、连接失败和结构错误映射为稳定中文错误，只暴露 provider ID 与错误类别。

### Cache and failure policy

每个 provider 的成功远程结果按 `discovery.ttl_seconds` 缓存。缓存只存在内存，不持久化 secret 或原始响应。

- fresh cache：立即返回，不访问凭据或网络。
- concurrent miss：创建一个 task，其余调用用 `asyncio.shield()` 等待同一 task；调用方取消不会取消共享刷新。
- expired cache + refresh failure：返回旧远程模型，`stale=true` 并附安全告警。
- first refresh failure：返回静态模型和告警；失败结果缓存 30 秒，防止高并发重试风暴。
- `refresh=true`：跳过 fresh/negative TTL，但仍参与 single-flight。

### Merge and visibility

静态可见模型按 catalog 声明顺序保留，静态模型的名称、上下文、输出和能力元数据优先。远程结果按模型 ID 的 Unicode codepoint 稳定排序。

若远程 ID 已等于某个静态模型的 upstream ID，则不额外加入一个重复的远程条目。合并后对本地 ID 应用 whitelist/blacklist：白名单非空时只显示列出的 ID，黑名单始终排除。

### Router dynamic overlay

`resolve_model_target()` 增加可选的动态模型映射。`ModelRouter` 保存最近一次发现成功或 stale 的 provider overlay，并提供：

```python
async def list_available_models(
    provider_id: str | None = None,
    *,
    refresh: bool = False,
) -> tuple[ProviderModelListing, ...]
```

静态模型调用不触发发现。catalog provider 下一个未声明模型进入 `call()` / `stream()` 时，Router 只发现该 provider；模型确实存在才写入 overlay 并继续 transport，未发现或发现失败则返回安全的 `ModelResolutionError`。因此自动列表不是装饰数据，而是可执行路由事实。

启动欢迎界面使用的同步 runtime identity 对 discovery-only 默认模型返回 `source=catalog_pending`，显示 provider、请求模型和 API format，但不提前联网。真实调用前仍必须通过发现验证。

## User Surfaces

### Slash command

新增 `/models [provider] [--refresh]`，与现有 `/model`（显示三档配置）并存。命令由共享 slash backend 执行，因此 CLI 兼容层、新 UI 和 TUI 输出一致。

输出按 provider 分组，模型显示 `provider/local-id`、名称、`静态`/`发现`来源；每个 provider 最多默认显示 100 项并报告省略数量。stale 与失败回退使用一行中文告警，绝不显示认证头或响应正文。

### REST config

`GET /config` 改为调用同一 `list_available_models()`，移除硬编码 Kimi。`ModelInfo` 增加 `upstream_id`、`source` 与可选能力字段；`ConfigResponse` 增加 `model_warnings`。API 发现失败仍返回 200 + 静态列表和告警，不把 provider 临时不可用升级成整个配置页失败。

专用交互式模型选择器属于下一切片；本轮通过 `/models` 已让新 UI 与 TUI 看到真实列表。

## Testing

- 纯解析测试覆盖 OpenAI、Ollama、重复、无效条目、500 项上限和畸形 envelope。
- 本地 loopback HTTP server 验证真实 headers、URL、2 MiB 中止、状态码与无 secret 错误。
- 可控时钟测试 fresh、expiry、stale-if-error、30 秒失败缓存与 `refresh=true`。
- 并发测试发起 50 个调用，证明服务器只收到一次请求；取消一个 waiter 不取消共享请求。
- 合并测试覆盖静态 upstream 去重、声明顺序、远程排序、白名单和黑名单。
- Router 测试证明发现模型可以 `call()` / `stream()`，未知模型不被盲目透传，静态模型不联网。
- slash/API 测试证明新 UI/TUI 共享命令输出，REST 不再硬编码 Kimi。
- 真实场景分别运行本地 OpenAI `/models` 与 Ollama `/api/tags` loopback 服务，不调用外部厂商或真实 Keychain。

## Boundaries and Follow-ups

- 本轮不实现 Google GenAI、Azure deployment、Anthropic 或云厂商专用发现响应。
- 本轮不下载 models.dev，不补价格与最新上下文元数据。
- 本轮不持久化远程列表到磁盘。
- 本轮不实现交互式 provider/model picker；`/models` 和 REST 是首个用户入口。
- 下一步依次实现 Google GenAI adapter、Ollama inference adapter、models.dev metadata fallback 与新 UI picker。

## Self Review

设计没有把“请求成功”当成“所有模型都可用”，也没有让 discovery-only provider 任意透传拼写错误。静态模型保持零网络路径；动态模型必须由真实端点证明。缓存、失败回退和 single-flight 解决高并发稳定性，响应限幅与安全错误覆盖不可信 provider。范围保持在一个端到端能力，未把 Google/Azure 的不同协议伪装成 OpenAI。

