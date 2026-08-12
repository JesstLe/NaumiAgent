# EVO-06.4a Sealed Shadow Descriptor

## 目标

把 EVO-06.3b2c active catalog lease 转换成后续离线反事实路由评价的确定性输入，但不调用模型、不加载
候选代码、不注册 Tool、不产生工具副作用。该 descriptor 是数据契约，不是 Shadow authority；只有实时 View
重新验证同一 lease 和全部来源后，才暂时声明 `offline_shadow_input_eligible=true`。

## 来源链

编译必须同时满足：

1. 当前 Runtime 持有 active、locally reserved Registry lease；detached lease 不可编译；
2. Candidate 与 Capability Proposal 仍由当前 authority、cooldown 和全局 Portfolio 产生；
3. Candidate hypothesis 已通过凭据、控制字符和本机绝对路径过滤，作为真实 routing description；
4. 五步 Capability Specification 为 current/complete；parameters/result/error/permission/scenario 均已冻结；
5. Implementation Artifact 为 current `preview_ready`，且其 Specification/Artifact ID 与摘要和 lease 完全一致。

不使用通用占位描述，也不让 LLM 生成新的工具说明。路由语义必须来自已持久化、content-addressed 的
Candidate 与 Specification 证据。

## Descriptor 契约

`EvolutionCapabilityShadowDescriptor` 封存：

- Candidate revision/digest；Registry lease ID/digest/expiry；
- Artifact 与 Specification ID/digest；
- declared tool name 和 provider-safe evaluation tool name；
- 脱敏 routing description、完整 parameters schema 及其摘要；
- result schema 摘要、稳定 error codes、permission families 和真实 scenario names；
- `evaluation_mode=counterfactual_recommendation_only`。

不可变安全位全部 fail closed：

- `offline_shadow_input_authorized=false`；
- `production_model_visible=false`；
- `registry_resolvable=false`；
- `side_effects_allowed=false`；
- `execution_authorized=false`；
- `activation_authorized=false`。

evaluation name 使用完整 96-bit Candidate ID 前缀并限制为 64 字符 provider-safe 字符集。它只供后续隔离评价
使用，不能替换 declared name 或写入生产 ToolRegistry。

## 动态 View 与撤权

View 每次读取都会重验 Registry lease、Candidate、Specification、Artifact 和 routing description：

- `ready`：同一 active 本地 lease 且全部来源 current；仅此状态可作为后续离线评价输入；
- `detached`：lease 属于其他存活 Runtime，本进程不得评价；
- `released/revoked/expired`：继承 lease 机械终态；
- `source_revoked`：lease identity、Candidate、Specification、Artifact 或 description 漂移；
- `missing`：尚未编译。

descriptor 本身永久保持未授权，避免旧对象在 lease 到期后被误用。后续 evaluator 必须消费 Service 的实时
View，不能只读取 SQLite JSON。

## Store 与并发

Store 以 `(workspace_root, lease_id)` 唯一绑定 descriptor，采用 `BEGIN IMMEDIATE` 和不可覆盖语义。两个独立
Service/Store 并发编译同一 lease 时只能得到同一 content-addressed descriptor。JSON 与持久摘要不一致时
fail closed，不返回降级对象。

## 双通道和界面

- Agent Tool：`evolution_capability_shadow_descriptor`，支持 `inspect/compile`；
- CLI/TUI fallback：`/evolution capability-shadow` 与 `capability-shadow-status`；
- New UI：同名 typed action，compile 通过 Agent Tool 的同一权限入口；
- 用户可见回执明确区分“离线 Shadow 输入”和“生产模型可见/Registry 可解析/可执行”。

## 验收证据

- [x] 真实 ARC-04 passed Receipt → active Registry lease → Shadow descriptor 完整链路；
- [x] 两个独立 Store 并发编译得到完全相同的 descriptor；
- [x] descriptor 不含 Candidate source，不进入 Registry names/OpenAI tools；
- [x] 源码漂移、lease detached、revoked 与 expired 均撤销实时 eligibility；
- [x] descriptor payload 摘要篡改 fail closed；
- [x] Agent Tool、CLI/TUI fallback 和 New UI typed action 使用同一 compile 服务；
- [x] Ruff、py_compile、Python/Node 小模块测试、真实 E2E 与文档治理通过；不运行全量测试。

## 下一步

[EVO-06.4b](EVO-06-4b-shadow-observation-contract.md) 已实现 Shadow Observation Contract：绑定 current
4a View、完整基线工具目录、interaction-backed 正样本、真实负控制、model/provider capability contract、
固定 reasoning/采样参数和 token/时间/成本预算；持久契约仍不调用 Provider 或执行候选 Tool。EVO-06.4c1
下一步实现 bounded Observation Runner，4c2 再聚合 routing precision/recall、false-positive、成本与稳定性；
未达到门槛不得进入 Limited Activation。
