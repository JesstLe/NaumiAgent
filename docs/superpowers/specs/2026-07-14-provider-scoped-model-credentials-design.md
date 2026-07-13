# Provider-Scoped Model Credentials Design

## Goal

让多个模型厂商的凭据可以同时安全保存并按显式 provider 精确加载，避免切换厂商时覆盖旧 Key，也避免残留环境变量或旧全局 Key 抢占当前 provider。

这是多厂商 provider catalog 的第一个独立基础能力。本轮不实现模型列表发现、JSON catalog 或新 API adapter；这些能力将在凭据身份稳定后逐个提交。

## Existing Behavior

- `ModelConfig` 已有 `provider` 字段，但系统凭据库始终使用单一账号 `models.api_key`。
- `naumi configure --provider ...` 和 onboarding 每次都会覆盖这一个账号。
- `AppConfig.from_yaml()` 不把 provider 传给凭据加载器，因此无法区分 OpenAI、Anthropic、Kimi 或自定义端点。
- 显式环境变量 `NAUMI_MODELS__API_KEY` 会优先于凭据库，这是正确行为，应继续保留。

## Reference Principles

OpenCode 使用 provider ID 作为配置和模型引用的稳定命名空间。Hermes 的近期 provider 问题则暴露了两个反例：按 base URL 猜 provider 会选错凭据；残留全局环境变量可能覆盖显式 provider。NaumiAgent 采用以下不可变优先级：

1. 当前进程显式注入的 `NAUMI_MODELS__API_KEY`。
2. 当前配置中显式 provider 对应的系统凭据。
3. 旧版全局 `models.api_key`，仅作为兼容回退。
4. 不按 base URL 或其他厂商环境变量猜测 Key。

## Credential Identity

- 旧账号保留为 `models.api_key`。
- provider 账号格式为 `models.providers.<provider-id>.api_key`。
- provider ID 统一转小写，允许 ASCII 字母、数字、点、下划线和短横线，首字符必须是字母或数字，最长 64 字符。
- 空值、路径分隔符、空格、冒号和其他不稳定字符被拒绝，避免凭据账号混淆。

公开 API 保持向后兼容：

```python
store_model_api_key(value, provider=None, backend=None)
load_model_api_key(provider=None, backend=None, fallback_to_legacy=True)
```

不传 provider 时继续读写旧账号。传 provider 时只写 provider 账号；读取 provider 账号为空时可回退旧账号，但绝不自动复制或删除旧凭据，避免一次读取产生系统写操作或钥匙串确认。

## Configuration Flow

### AppConfig

`AppConfig.from_yaml()` 完成 YAML 与环境变量解析后：

- 如果 `models.api_key` 已由环境变量或显式配置提供，不访问系统凭据库。
- 否则只为 `models.provider` 加载对应凭据；provider 缺失时读取旧账号。
- provider 账号缺失时读取旧账号兼容现有安装。

系统不会枚举或预加载所有 provider 的 Key，因此启动一次最多读取当前 provider，不会因未来 catalog 中存在多个厂商而触发多次系统凭据提示。

同一进程对同一“凭据后端实例 + 账号”的读取结果（包括不存在）做内存缓存，`needs_onboarding` 与正式配置加载共享结果，避免同次启动重复触发 Keychain。缓存不写磁盘；成功保存新 Key 时立即更新对应项，后端异常不进入缓存。

### Configure / Onboarding

- `configure_project(provider=...)` 把新 Key 写入该 provider 账号。
- onboarding 在用户选定 provider 后把 Key 写入同名账号。
- 明文旧 Key 迁移时，如果 YAML 已有 provider，就写入对应账号；否则仍写入旧账号。
- 成功写入系统凭据后才删除 YAML 明文，保持原子语义。

## Error Handling

- provider ID 无效时，在访问钥匙串前返回中文 `ValueError`。
- 系统凭据读写异常统一包装为 `CredentialStoreError`，不得包含 Key、账号内部值或后端异常明文。
- provider Key 缺失不是错误；只有后端访问失败才是错误。
- provider 读取失败时不得静默回退旧账号，因为这会掩盖系统凭据故障。

## Compatibility

- 现有 `store_model_api_key(value)` 与 `load_model_api_key()` 行为不变。
- 旧 `models.api_key` 继续作为读取回退，已有用户无需立即迁移。
- 环境变量仍为当前进程最高优先级，CI/测试继续使用 `NAUMI_MODELS__API_KEY=unit-test-placeholder`，不会访问 Keychain。
- Keyring service name 保持 `NaumiAgent`，不产生第二个应用授权项。

## Verification

- 同一后端同时保存 Kimi、OpenAI、Anthropic，读取互不覆盖。
- provider 大小写归一化，非法 ID 在后端调用前被拒绝。
- provider Key 缺失时回退旧账号；关闭回退时返回 `None`。
- YAML 显式 provider 只请求对应 provider 凭据；环境变量存在时完全不访问 Keychain。
- configure、onboarding、明文迁移写入正确 provider。
- 仅运行 credentials/config/configurator/onboarding 小模块测试与 Ruff，不运行全量测试。

## Deferred Work

1. OpenCode 形状的 provider JSON catalog 与内置厂商清单。
2. API 格式归一化：OpenAI Chat、OpenAI Responses、Anthropic Messages、Google GenAI、Azure OpenAI、Ollama/vLLM。
3. provider/model 别名映射与 tier 选择。
4. `/models`、Ollama tags 等自动模型发现、缓存、白名单和黑名单。
5. 新 UI/TUI 的 provider 连接与模型选择界面。
