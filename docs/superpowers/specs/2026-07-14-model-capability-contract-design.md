# 模型能力契约与可信度设计

## 目标

新增 provider 或模型时，不仅“能填 ID”，还必须知道运行时采用了哪些限制、能力和价格，数据来自哪里，以及哪些仍是猜测。静态错误在启动前拒绝，无法从通用 `/models` 接口确认的事实明确标为未验证。

## 契约字段

每个模型形成统一 `ModelCapabilityContract`：

- canonical/upstream/provider/API format；
- context window、max output、当前请求 max tokens；
- tools、reasoning、vision、input/output modalities；
- input/output 单价；
- 每个字段的来源：`config`、`catalog`、`litellm` 或 `fallback`；
- 总状态：`verified`、`partial`、`unverified`、`incompatible`；
- 中文 warnings/errors。

## 静态不变量

- 所有 token 限制必须为正整数，价格不得为负；
- `max_output <= max_context`；
- 当前请求 `max_tokens` 不得超过模型输出上限；全局配置过大时沿用现有安全收紧并报告 warning；
- tools 明确为 false 时，对完整 Agent Harness 报告 incompatible；
- reasoning efforts/default 保持现有集合约束；
- 模态数组不得重复；vision 与显式 image 输入声明不得互相矛盾。

## 可信度

- config/catalog 明确声明或 LiteLLM registry 精确命中的限制可作为已知值；fallback 只能保证系统有保守默认，不能称为 provider 真实规格。
- 远程 `/models` 仅返回 ID 时，新模型为 unverified；用户应在 catalog 或 `models.model_info` 补齐限制与能力。
- provider live probe 只在用户显式 `doctor --live` 时执行；本切片不通过收费请求猜测规格。

## 展示

- Bridge status 暴露安全的 capability contract，不含密钥或原始 provider 响应。
- Doctor 对 configured fast/capable/reasoning tiers 汇总契约状态。
- 新 UI 只在 partial/unverified/incompatible 时显示紧凑警告；详细来源进入 Doctor/Inspector，避免底栏过载。

## 验证

配置与 catalog 边界测试、Router 来源优先级测试、未知发现模型测试、Doctor 与 Bridge 状态测试、新 UI 警告渲染测试。仅运行模型/Doctor/Bridge/状态栏相关定向测试。
