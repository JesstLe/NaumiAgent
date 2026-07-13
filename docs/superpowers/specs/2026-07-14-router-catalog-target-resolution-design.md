# Router Catalog Target Resolution Design

## Goal

把已经验证的 provider JSON catalog 接入 Naumi 运行时组合根，让 `ModelRouter` 能把配置模型或显式模型名稳定解析为 provider、本地别名和 upstream model，并让上下文窗口等元数据真正使用 catalog 声明。

本轮只完成目标解析与元数据接入，不改变 `call()` / `stream()` 的底层请求模型、endpoint、adapter 或凭据。这样不会在 adapter 尚未验证时把 catalog alias 错发给 LiteLLM，也不会让全局活动凭据串到另一个 provider。

## Runtime Composition

`ModelConfig` 新增可选字段：

```yaml
models:
  provider: nvidia
  catalog_path: providers.json
  default_model: z-ai/glm4.7
```

`AppConfig.from_yaml()` 将相对 `catalog_path` 锚定到 YAML 所在目录，规则与 SQLite、Chroma 路径一致。`AgentEngine` 是生产组合根：配置了路径时调用 `load_provider_catalog()`，将不可变 catalog 注入 `ModelRouter(config.models, catalog=catalog)`；未配置时保持当前构造与行为。

catalog 文件不存在、超过限制或内容错误继续抛出已有 `ProviderCatalogError`。加载阶段不解析密钥、不读环境变量、不访问 Keychain、不发网络。

## Resolved Target

新增不可变 `ResolvedModelTarget`：

- `requested_model`: 去除首尾空白后的用户原文。
- `canonical_model`: catalog 目标为 `provider/local-alias`；legacy 目标保持原文。
- `upstream_model`: catalog 的 `ProviderModelSpec.upstream_id`；legacy 目标保持原文。
- `provider`: catalog 目标的 `ProviderSpec`，legacy 为 `None`。
- `model`: catalog 目标的 `ProviderModelSpec`，legacy 为 `None`。
- `source`: `catalog` 或 `legacy`。

`ModelRouter.resolve_target(model)` 始终返回这个对象。现有 `resolve_model(tier) -> str` 完全不改，避免破坏 Engine、Compactor、UI、Doctor 等现有调用方。

## Resolution Rules

### Qualified model

`provider/alias` 只按第一个 `/` 拆分，因此 alias 自身可以继续包含 `/`。如果首段命中 catalog provider：

1. alias 在 `visible_models()` 中存在：返回 catalog target。
2. alias 在 provider 的静态 `models` 中存在、但被 whitelist/blacklist 隐藏：抛出 `ModelResolutionError`，说明模型被可见性规则过滤。
3. alias 不存在：抛出 `ModelResolutionError`，说明 provider 未声明该别名。

不会用 `upstream_id` 反向匹配 alias，防止多个本地别名映射同一上游模型时产生歧义或绕过过滤。

### Unqualified model

只有 `ModelConfig.provider` 命中 catalog provider 时，才在该 provider 下查找同名本地 alias。存在则返回 catalog target；静态存在但被过滤或完全不存在时抛出对应错误。

如果活动 provider 未命中 catalog，模型按 legacy 原样返回。不会跨所有 provider 自动搜索，因为多厂商中同名模型很常见，隐式猜测会造成凭据和 endpoint 串线。

### Empty input

空字符串或纯空白抛出中文 `ModelResolutionError`。错误只包含安全的 provider/model ID，不包含 auth 引用、header、catalog JSON 或密钥。

## Metadata Resolution

`get_model_info(requested)` 继续按请求字符串缓存，但改为逐字段合并：

1. fallback 默认上下文、输出和价格。
2. LiteLLM 使用 `target.upstream_model` 查询内置元数据，覆盖可用字段。
3. catalog 的 `max_context` / `max_output` 覆盖对应字段。
4. `ModelConfig.model_info[target.canonical_model]` 逐字段覆盖。
5. `ModelConfig.model_info[target.requested_model]` 逐字段覆盖。

当 requested 与 canonical 相同时只应用一次。输入价、输出价分别合并，允许用户只覆盖其中一个价格而保留另一来源，不再要求四项元数据成套出现。

对 legacy 目标，canonical/upstream 都等于原文，最终效果保持现有 config → LiteLLM → fallback 语义。

## Boundaries

- `api_format` 和 `base_url` 可以为 `None`，target 仍可解析；能否执行由下一轮 adapter registry 判断。
- `call()` / `stream()` 本轮不把 alias 改成 upstream model。
- 不读取或解析 credential/env/file secret reference。
- 不执行 `/models` discovery，不合并远端模型。
- 不新增 UI/TUI picker；后续 picker 复用同一 target/canonical 规则。

## Verification

定向单测覆盖：

- qualified alias 与含 `/` alias 映射 upstream。
- unqualified alias 仅在活动 provider 内解析。
- provider 未命中时 legacy 原样透传。
- 未知 alias、被过滤 alias、空输入的中文错误。
- 同一 upstream 的不同 alias 不反向混淆。
- OpenCode 内置 provider 的 `api_format/base_url=None` 仍可解析。
- 元数据逐字段优先级与缓存隔离。
- `resolve_model()` 继续返回原字符串。
- `catalog_path` 相对 YAML 锚定。
- Engine 有/无 catalog 的注入路径。

真实场景使用临时 YAML + 多 provider JSON 构造 `AppConfig` 与 `AgentEngine`，验证 Router target，不访问网络；另用本机 OpenCode catalog 直接解析 `nvidia/z-ai/glm4.7` 和 `zhipuai-coding-plan/glm-5.1`。

只运行 catalog、Router、config 与 Engine 的命名小测试，并固定 `NAUMI_MODELS__API_KEY=unit-test-placeholder`，避免 Keychain 提示。

## Deferred Work

1. OpenAI-compatible Chat adapter：`openai/<upstream>`、base URL、headers、timeout 与 provider 凭据。
2. OpenAI Responses adapter：显式 `openai/responses/<upstream>`，不能靠 URL 推断。
3. Anthropic、Google GenAI、Azure OpenAI、Ollama adapters。
4. Secret reference resolver 与多 provider 凭据切换。
5. 模型发现、缓存、过滤合并和 UI/TUI picker。
