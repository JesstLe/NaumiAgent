# HAR-08.5c2 Provider 成本与账单证据来源

## 状态

已实现。本切片修复 HAR-08.5a-5c1 的证据语义：`ModelRouter` 的 `cost_usd` 一直由响应 token
乘以能力合同中的 rate card 单价得到，它是本地可复算的成本估算，不是 Provider 最终账单。Live Eval 现在把
usage、cost、billing 和 Provider response id 的来源分别记录，New UI/TUI 不再把估算值称为实际账单。

## 为什么是 5c1 之后的最小依赖

- 5c1 已把成本推送到双端，若不先建立来源合同，继续做取消或历史页会持久化错误语义。
- `ModelResponse` 已是 ModelPort 的共享返回值；增加冻结的调用证据不需要扩张 ModelPort 方法集。
- H5a 已有 Live evidence，可在保持旧 JSON 摘要稳定的前提下追加非默认来源事实。
- Provider 账单 API 和远端取消需要 Provider 专用凭据、原始请求标识与对账周期，不能由本地 timeout 推断。

## 调用证据合同

`ModelCallEvidence` 是冻结、严格校验的低敏合同：

| 字段 | 闭集 | 含义 |
| --- | --- | --- |
| `provider_response_id_sha256` | 空或 64 位 SHA-256 | Provider 响应标识的摘要；不保存原值 |
| `usage_source` | `transport_response` / `unavailable` | token 是否来自 transport response |
| `cost_source` | `rate_card_estimate` / `provider_billing` / `unavailable` | 金额的证据来源 |
| `rate_card_source` | `catalog` / `config` / `litellm` / `mixed` / `fallback` / `unavailable` | 估算所用单价来源 |
| `billing_status` | `supported` / `unsupported` / `unavailable` | 当前适配器是否取得账单证据 |

`provider_billing` 与 `supported` 必须同时出现；`rate_card_estimate` 必须绑定 transport response 用量及
明确单价来源，严格 Live Eval 拒绝 `fallback/unavailable`。
当前 `ModelRouter` 的真实状态是：

```text
usage_source   = transport_response
cost_source    = rate_card_estimate
rate_card_source = catalog | config | litellm | mixed
billing_status = unsupported
```

这不否定预算保护：Live Eval 调用前仍使用可信 capability 单价计算最坏上界，调用后使用同一 rate-card
合同和真实返回 token 计算已记录估算。它只禁止把该估算冒充 Provider 发票或最终扣款。

## Live 与 H5a 传播

单次 `HarnessLiveEvalReceipt` 绑定调用证据并进入自身防篡改摘要。自定义 ModelPort 如果返回非零用量和成本、
却未声明来源，Live Eval 以 `usage_provenance_unavailable` 或 `cost_provenance_unavailable` 收口为 partial，
不得生成通过结论。

每个 H5a `HarnessEvalLiveEvidence` 保存同样的四类事实。为保护既有不可变记录：

- 旧记录缺失这些字段时解析为 `unavailable`；
- 默认 `unavailable` 和空 response-id 摘要不参与 JSON 序列化；
- 因此旧 H5a payload 的 canonical shape 和历史 digest 不变；
- 新执行取得的非默认来源事实正常进入 result digest。

同理，New UI 解析 5c1 历史 Live event 时把缺失来源降级为 `unavailable/0`；显式声明的新字段仍执行严格
闭集与交叉一致性校验。

Batch 聚合额外记录：

- `cost_source`：rate-card 估算、Provider 账单、`mixed` 或 `unavailable`；
- `rate_card_source`：catalog/config/LiteLLM 单价来源、`mixed` 或 `unavailable`；
- `billing_status`：单一状态、`mixed` 或 `unavailable`；
- `provider_response_ids_observed`：取得安全摘要的调用数，且不得超过 `total_calls`。

协议 v1 继续保留字段名 `actual_cost_exceeded` 以兼容 5c1，但其机械含义是
`total_cost_usd > max_total_cost_usd`，即“当前记录金额超出预算”。UI 和文档不得把它解释成最终账单超支。
稳定错误码改为 `observed_cost_exceeded`。

## 双端体验

New UI 的 Live Batch 页面显示：

- `能力单价估算成本`、`Provider 账单成本`、`混合来源成本`或`成本来源不可用`；
- 估算使用的 Provider catalog、用户配置、LiteLLM 元数据或混合单价来源；
- 当前适配器的账单证据状态；
- Provider Response ID 摘要覆盖数，例如 `4/5`；
- 原有预算颜色、模型、token、identity 和隐私边界。

Textual TUI 状态栏使用同源字段显示“估算/账单/混合成本”和“账单未集成”等短标签，不解析 Tool 文案。

## 验收标准

- Router 对真实 transport response 生成 response-id SHA-256、用量来源、catalog 估算来源和未集成账单状态。
- 非法 digest、伪造来源、`provider_billing`/`supported` 不一致均被机械拒绝。
- Live runner 不接受未证明来源的自定义 ModelResponse。
- 单次回执、H5a、Batch terminal/progress 和双端 UI 使用同一来源事实。
- Provider response id 原文、Prompt、输出、reasoning 和私有异常不进入 UI/H5a。
- 旧 H5a Live evidence 在新增默认字段后保持原 canonical JSON shape。
- 只运行相关 Model/Live/H5a/UI 小模块测试，不调用真实付费 Provider，不运行全量测试。

## 未完成

1. 当前所有 ModelRouter Provider adapter 的 `billing_status` 均为 `unsupported`；尚无账单 API 集成。
2. 仅保存 response-id 摘要，尚无加密的原始 Provider 请求标识 authority，不能自动请求账单 API。
3. Provider 最终账单可能异步产生，尚无 polling、幂等对账、差额回执或退款处理。
4. Provider 远端取消证明仍未实现，本地 timeout/cancel 不能冒充远端停止。
5. macOS/Linux/Windows 与 OpenAI/Anthropic/Google/兼容网关的小额真实矩阵仍未执行。

下一切片应先设计安全的 Provider reconciliation handle：原始标识必须加密、按 workspace 隔离、有 TTL，
且只有明确支持账单查询的 adapter 才能把 `billing_status` 提升为 `supported`。远端取消随后复用该 handle，
不能在没有 Provider 证明时仅修改 UI 状态。
