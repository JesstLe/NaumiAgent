# Brave 高级搜索配置设计

## 目标

在 `.naumi/config.yaml` 中提供稳定、可验证的搜索配置，同时禁止把 Brave API Key 明文写入仓库。现有零配置 DuckDuckGo 与浏览器回退继续可用。

## 配置契约

```yaml
search:
  provider_order: [brave, duckduckgo, browser]
  brave:
    enabled: true
    api_key_ref: "{env:BRAVE_SEARCH_API_KEY}"
    country: CN
    search_lang: zh-hans
    ui_lang: zh-CN
    safesearch: moderate
    spellcheck: true
    freshness: null
    timeout_seconds: 10
```

- `api_key_ref` 仅允许 `{env:VARIABLE_NAME}`；直接填写 token 必须在配置校验阶段拒绝，错误中不得回显 token。
- 默认引用 `BRAVE_SEARCH_API_KEY`，所以现有用户无需迁移。
- `provider_order` 只允许 `brave`、`duckduckgo`、`browser`，不允许重复或空数组。
- Brave 未启用或引用的环境变量为空时跳过 Brave，继续零配置 provider。
- 地区、搜索语言、UI 语言、`safesearch`、拼写修正、freshness 和超时统一从配置进入请求。参数遵循 Brave Web Search 官方契约。

## 运行时与安全

- `AppConfig` 保存引用，不保存解析后的搜索 token；搜索工具执行时才从进程环境读取。
- Engine 将同一个 `SearchConfig` 注入 `WebSearchTool`，Doctor 也使用同一解析方法，避免“诊断说已增强但工具没用上”。
- 请求、错误、日志、Doctor、完成回执和 UI 均不得输出 token。
- Brave 认证、限流、超时、空结果仍按 provider 顺序自动回退。

## 验证

- 配置测试覆盖默认值、完整 YAML、非法 provider、重复顺序、明文密钥和非法 locale/freshness。
- 工具测试覆盖自定义环境变量引用、禁用 Brave、请求参数、provider 顺序与无密钥回退。
- Doctor 测试覆盖默认/自定义引用和密钥脱敏。
- 用本地 HTTP transport fixture 检查请求参数与认证 header，不调用计费 API。
- 仅运行配置、搜索工具、Doctor 和 Engine 注册的定向测试。
