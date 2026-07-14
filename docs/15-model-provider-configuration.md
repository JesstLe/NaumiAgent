# 模型、Provider 与思考强度配置

NaumiAgent 的项目配置默认位于 `.naumi/config.yaml`。模型 provider 目录默认建议放在
`.naumi/providers.json`，会话与向量数据位于 `.naumi/data/`。旧项目根目录的
`config.yaml` 仍可兼容读取，但新项目不再把 Naumi 专属状态散落到项目根目录。

模型密钥不写入任何 YAML 或 JSON。使用 `naumi configure` 保存到系统凭据库，或通过
`NAUMI_MODELS__API_KEY` 等环境变量注入。

## 思考强度与思考文本

这两个概念彼此独立：

- `/effort` 控制请求给模型的推理计算强度。
- `/reasoning` 控制界面是否显示模型返回的 reasoning/thinking 文本。

支持的统一强度名称为 `none`、`minimal`、`low`、`medium`、`high`、`xhigh`、`max`。
`auto` 表示 NaumiAgent 不发送强度参数，使用供应商默认值。并非每个模型都支持全部档位；
NaumiAgent 只发送该模型明确声明的值，不按模型名称猜测。

常用命令：

```text
/effort                  # 查看当前模型的有效强度、来源和可选值
/effort high             # 本进程临时切换为 high
/effort auto             # 本进程临时改为供应商默认
/effort reset            # 清除临时覆盖，恢复 YAML 配置
/reasoning on            # 显示模型返回的思考文本
/reasoning off           # 隐藏模型返回的思考文本
```

`/effort` 的修改不会重写 `.naumi/config.yaml`。有效值的优先级为：临时覆盖、单模型配置、
全局配置、`auto`。

## 直连模型配置

不使用 provider catalog 时，可以在 `model_info` 中同时声明能力和选择：

```yaml
models:
  default_model: "anthropic/claude-opus-4-6"
  fast_model: "anthropic/claude-haiku-4-5"
  reasoning_model: "anthropic/claude-opus-4-6"
  reasoning_effort: auto
  model_info:
    anthropic/claude-opus-4-6:
      reasoning_efforts: [low, medium, high, max]
      default_reasoning_effort: high
      reasoning_effort: high
```

`reasoning_efforts` 必须来自当前模型、API 与 LiteLLM 组合的真实支持范围。
`default_reasoning_effort` 必须出现在该数组中。当前锁定的 LiteLLM 传输已通过本地回环验证：
Claude 4.6 的 `medium` 与 `max` 会转换为原生 `output_config.effort` 和 adaptive thinking；
该传输当前拒绝 Claude `xhigh`，因此不要在 Claude 4.6 的能力列表中声明它。

启用显式强度后，NaumiAgent 会省略 `temperature`，避免与推理模型的采样限制冲突。显式
`thinking` 参数也不能与强度同时发送。Kimi 的二值 thinking 协议保持独立，在强度为
`auto` 时继续按原有规则工作。

## Provider catalog 配置

当前真正接入统一 Router 并可执行的 `apiFormat` 有：

| `apiFormat` | 请求协议 | LiteLLM transport |
|---|---|---|
| `openai_chat` | OpenAI-compatible Chat Completions | `openai/` |
| `openai_responses` | OpenAI Responses | `openai/responses/` |
| `anthropic_messages` | Anthropic Messages | `anthropic/` |
| `google_genai` | Google GenAI 原生 `generateContent` | `gemini/` |

Catalog 能识别但尚未实现 transport 的格式会在网络请求前返回中文错误，不会静默改走
其他协议。

`.naumi/config.yaml` 指向同目录的 provider catalog：

```yaml
models:
  provider: openai
  catalog_path: providers.json
  default_model: openai/reasoner
  reasoning_effort: high
```

`.naumi/providers.json` 在具体模型上声明能力：

```json
{
  "providers": {
    "openai": {
      "name": "OpenAI",
      "apiFormat": "openai_responses",
      "baseURL": "https://api.openai.com/v1",
      "auth": {
        "type": "bearer",
        "env": "OPENAI_API_KEY"
      },
      "models": {
        "reasoner": {
          "upstreamId": "gpt-5",
          "capabilities": {
            "tools": true,
            "reasoning": {
              "efforts": ["none", "minimal", "low", "medium", "high"],
              "defaultEffort": "medium"
            }
          }
        }
      }
    }
  }
}
```

能力数组保持声明顺序，不能为空、不能重复，也不能包含 `auto`。旧格式
`"reasoning": true` 仍兼容，但它只表示“模型支持推理”，没有足够信息开放可选强度；
此时 `/effort` 只允许安全使用 `auto`。

远程 `/models` 发现通常只返回模型 ID，不返回可靠的强度能力。因此，纯远程发现的模型不会
自动继承或猜测强度；需要在静态 catalog 或 `model_info` 中补充能力元数据。

### Google GenAI 原生配置

Google AI Studio 使用原生 `generateContent` 协议时，`.naumi/config.yaml` 可以写为：

```yaml
models:
  provider: google
  catalog_path: providers.json
  default_model: google/gemini-fast
  fast_model: google/gemini-fast
  reasoning_model: google/gemini-fast
  reasoning_effort: auto
```

同目录的 `.naumi/providers.json`：

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
        "credentialProvider": "google",
        "header": "X-Goog-Api-Key"
      },
      "models": {
        "gemini-fast": {
          "upstreamId": "gemini-3.5-flash",
          "name": "Gemini 3.5 Flash",
          "capabilities": {
            "tools": true
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

`credentialProvider: "google"` 从系统凭据库读取 provider-scoped 密钥，可先运行
`naumi configure --provider google` 保存。自动化环境也可以改成
`"env": "GEMINI_API_KEY"`；JSON 中始终只写引用，不写真实 key。

启动后可刷新 Google 的当前模型列表：

```text
/models google --refresh
```

发现逻辑只展示声明支持 `generateContent` 的模型，并复用既有 TTL、single-flight、
stale-if-error 和响应大小边界。远端 ID 是当前 endpoint 的事实，但 `/models` 通常不提供
可信的上下文窗口、价格、工具与思考强度元数据；这些能力在 models.dev 接入之前仍应通过
静态模型声明补充。静态 alias 也便于在供应商 ID 变化时保持项目配置稳定。

动态发现或静态配置的 Google 模型都通过同一个 Router 执行。系统消息会保留为原生
`systemInstruction`，工具调用/工具结果、流式响应、finish reason 与 usage 会归一化到
NaumiAgent 的统一结构。标准 Google key、自定义 header、bearer 和明确的 `none` 模式都
不会回退读取未选中的系统环境密钥。`requestTimeoutMs` 必须是正整数，并同时约束模型
发现和推理请求；未声明时模型发现使用 10 秒安全默认值，推理由 LiteLLM 的默认超时管理。

## 界面与 API

新版 Terminal UI 欢迎页显示当前“思考强度”，底栏分别显示“思考文本”和“强度”。Textual
TUI 使用相同术语。`/model` 展示当前有效强度，`/models` 展示各静态模型的可选值与默认值。

REST `GET /config` 返回：

- 顶层 `reasoning_effort`：当前模型、有效值、来源、可选值、模型默认值与警告；
- 每个 `models[]`：`reasoning_efforts` 与 `default_reasoning_effort`。

强度状态由 `ModelRouter` 在每次请求前重新解析。切换模型后，如果临时或全局选择对新模型
无效，请求会在网络 I/O 之前以中文错误阻断，不会静默降级并让状态栏显示错误信息。

## 参考协议

- [OpenAI reasoning guide](https://developers.openai.com/api/docs/guides/reasoning)
- [Claude effort](https://platform.claude.com/docs/en/build-with-claude/effort)
- [Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai)
- [Gemini models](https://ai.google.dev/gemini-api/docs/models)
- [Gemini generateContent API](https://ai.google.dev/api/generate-content)
- [Gemini thinking](https://ai.google.dev/gemini-api/docs/generate-content/thinking)
