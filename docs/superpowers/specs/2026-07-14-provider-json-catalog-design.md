# Provider JSON Catalog Design

## Goal

建立一个独立、严格、无网络副作用的 provider/model JSON catalog 加载器，把 Naumi 原生配置和 OpenCode 风格 provider 配置归一化为同一套不可变运行时对象，为后续 Router adapter、模型映射和自动模型发现提供可信输入。

本轮只负责“读取、验证、归一化、筛选”。不访问 API、不读取真实 Key、不改变当前 `ModelRouter` 请求行为。

## Input Shapes

### Naumi native

```json
{
  "providers": {
    "nvidia": {
      "name": "NVIDIA NIM",
      "apiFormat": "openai_chat",
      "baseURL": "https://integrate.api.nvidia.com/v1",
      "auth": {
        "type": "bearer",
        "credentialProvider": "nvidia"
      },
      "models": {
        "z-ai/glm4.7": {
          "name": "GLM 4.7",
          "upstreamId": "z-ai/glm4.7",
          "limit": {"context": 128000, "output": 8192},
          "capabilities": {"tools": true, "reasoning": false}
        }
      },
      "discovery": {"enabled": true, "path": "/models", "ttlSeconds": 3600}
    }
  }
}
```

snake_case aliases are accepted for Naumi-authored files.

### OpenCode provider section

The loader also accepts a full OpenCode config and reads only its top-level `provider` mapping:

```json
{
  "provider": {
    "nvidia": {
      "name": "NVIDIA NIM",
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "https://integrate.api.nvidia.com/v1",
        "apiKey": "{file:secrets/nvidia_api_key}"
      },
      "models": {}
    }
  }
}
```

Known adapter hints are translated to Naumi API formats. Unknown `npm` packages require an explicit `apiFormat`; the Python runtime never installs or executes an npm provider package.

## Normalized Model

- `ProviderCatalog`: immutable mapping of normalized provider IDs.
- `ProviderSpec`: id, display name, API format, base URL, auth reference, static headers, model mapping, discovery policy, whitelist and blacklist.
- `ProviderModelSpec`: local model ID, upstream ID, display name, token limits and declared capabilities.
- `ProviderAuthSpec`: auth type plus a secret reference, never the secret itself.
- `ModelDiscoverySpec`: enabled flag, relative endpoint path and cache TTL.

Supported API format identifiers are declared now so later adapters share one vocabulary:

- `openai_chat`
- `openai_responses`
- `anthropic_messages`
- `google_genai`
- `azure_openai`
- `ollama`

Catalog acceptance does not imply the current Router already executes every format; adapter availability is checked in the next feature.

## Secret References

Plaintext credentials are forbidden anywhere in the catalog.

Accepted references:

- `auth.credentialProvider: "nvidia"` → Naumi provider-scoped Keychain account.
- `auth.env: "NVIDIA_API_KEY"` or `options.apiKeyEnv` → environment variable name.
- OpenCode-compatible `options.apiKey: "{env:NVIDIA_API_KEY}"`.
- OpenCode-compatible `options.apiKey: "{file:secrets/nvidia_api_key}"`; only the reference is stored. Path resolution and confinement are deferred to the credential resolver.

Any other `apiKey`, `api_key`, bearer token, Authorization header or X-API-Key header is rejected before a catalog object is produced. Error text names the field path but never echoes its value.

## Validation

- JSON UTF-8 size limit: 2 MiB.
- Duplicate JSON object keys are rejected, including nested provider/model keys.
- Provider IDs use the same stable ASCII namespace as provider-scoped credentials.
- Base URL must be HTTP(S), contain a host, and contain no username/password or fragment.
- Model IDs are non-empty, at most 256 characters and contain no control characters.
- Context/output limits must be positive integers when present.
- Headers must be string pairs and cannot directly carry sensitive authentication headers.
- Discovery path must start with `/`; TTL is 60–86,400 seconds.
- whitelist and blacklist cannot overlap.
- A provider must have at least one static model or enabled discovery.

Unknown fields inside provider/model/auth/options are rejected for Naumi native shape to catch typos. A full OpenCode root may contain unrelated application fields, which are ignored; provider fields explicitly understood by the importer are normalized, and unsupported runtime fields produce a clear error rather than being silently pretended to work.

## Visibility

`ProviderSpec.visible_models()` applies filters deterministically:

1. blacklist always removes matching IDs.
2. non-empty whitelist keeps only listed IDs.
3. results preserve JSON declaration order.

Filtering is separate from discovery merging so the same rules apply to static and remotely discovered models later.

## Error Handling

All failures raise `ProviderCatalogError` with a Chinese, field-oriented message and optional safe file path. JSON parser line/column may be included; raw catalog fragments, headers and secret values are never included.

## Verification

- Native and OpenCode fixtures normalize to identical provider/model semantics.
- Real local `ai-config-sync/opencode/opencode.json` provider section parses without reading its secret files.
- Inline secrets, duplicate keys, bad URLs, invalid IDs, bad limits, sensitive headers, conflicting filters and discovery-only boundaries have focused tests.
- A real JSON file with multiple providers is loaded and filtered; no network or Keychain function is called.
- Only the new catalog test module and targeted Ruff/import checks run.

## Deferred Work

1. Router target resolution and model alias mapping.
2. API-format adapter registry.
3. Secret reference resolution with provider-scoped Keychain/env/file confinement.
4. `/models` and Ollama discovery clients, caching and merge policy.
5. UI/TUI provider and model picker.

