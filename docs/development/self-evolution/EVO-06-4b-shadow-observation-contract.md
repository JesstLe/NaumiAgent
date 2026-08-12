# EVO-06.4b Shadow Observation Contract

## 目标

把 current EVO-06.4a Shadow descriptor 转换为后续隔离路由观察可以机械消费、动态撤权且有成本上界的输入契约。
本模块只冻结输入和协议，不调用 provider、不生成 observation、不加载或执行候选 Tool，也不授予 Limited
Activation。后续执行器只能消费 Service 返回的实时 `ready` View，不能直接信任 SQLite 中的旧 JSON。

## 依赖与边界

编译必须同时满足：

1. EVO-06.4a View 为 `ready`，且 exact descriptor ID/SHA、lease ID/SHA 当前有效；
2. 五步 Capability Specification 仍为 current `complete`，verification interaction 与 scenario 未漂移；
3. 当前生产 `ToolRegistry.get_openai_tools()` 可以形成完整、排序、无重复的 baseline；
4. 选定模型的 canonical identity、provider、API format、上下文、输出上限、价格、Tool 与 structured-output
   能力均来自非 fallback 的 `ModelCapabilityContract`；
5. reasoning effort 可以解析为固定值，reasoning 模型不得保留未知的 provider `auto`。

该阶段不依赖候选源码，不把候选加入 ToolRegistry，也不把 evaluation name 暴露给生产模型。

## 任务样本

### 正样本

正样本来自用户已经通过 Harness durable interaction 提交并由 Specification 校验的 verification scenario。
契约只保存 scenario name 与 fixture 的脱敏投影，并绑定 interaction ID/SHA、scenario index 和完整 scenario SHA；
不读取或保存原始 Goal objective、会话正文或自然语言 Tool Search 查询。

### 负控制

负控制来自 current baseline Tool 的真实公开 description。Service 以候选声明名称和 routing description 对
baseline 名称/描述执行确定性 token overlap 排序，排除 Evolution/Harness 控制 Tool、`tool_search`、
`request_user_input` 和不安全描述，最多选择 8 个 comparison tools；前 4 个形成 `not_recommend` 控制样本。

每份契约必须同时存在 `recommend` 正样本和 `not_recommend` 负控制，否则 fail closed。样本 ID 由来源、文本、
期望标签和 comparison tool 集合共同寻址；不能通过修改展示文本保留旧 identity。
`expected_recommendation` 只供 4c2 评分，4c1 Runner 构造 provider request 时必须剥离该字段，禁止标签泄漏。

## Baseline Tool Catalog

契约不复制候选代码，也不把完整生产 schema 当成可执行能力。它对每个 baseline Tool 保存：

- exact public name；
- 完整 OpenAI Tool schema SHA-256；
- description SHA-256。

`baseline_catalog_sha256` 对排序后的完整 OpenAI Tool schemas 计算。后续 observer 必须从当前 Registry 重建
schema 并逐字节匹配摘要；新增、删除、替换描述或修改 parameters schema 都会令旧契约进入
`catalog_changed`。

## Model 与采样契约

`ShadowObservationModelContract` 冻结：

- requested/canonical/upstream model、provider 与 API format；
- context/output/request token 上限及每字段 provenance；
- input/output rate card；
- tools、streaming、parallel tools、structured output、reasoning、vision 与 modalities；
- `verified/partial` 状态、warnings 和 content SHA。

关键字段不得来自 `fallback`。模型必须支持 text 输入输出、Tool schema 与 structured output，至少具备 4096
context 和 256 output tokens。

`ShadowObservationSamplingPolicy` 固定：

- `temperature=0`；
- 每个样本最多一次 provider call，禁止 parallel tool call；
- 单次最多 8192 input tokens，并保留 1024 context tokens；
- 单次最多 256 output tokens；
- 每次 60 秒，整体 wall-clock 为样本数乘 60 秒；
- 用可信 rate card 和最大 token 数向上取整形成 `max_cost_microusd`；
- 不请求 raw chain-of-thought。

唯一允许的结构化输出字段为：

- `recommendation`：`recommend | not_recommend | indeterminate`；
- `reason_codes`：固定枚举、1..4 个、不可重复；
- `evidence_tool_names`：本轮比较涉及的公开 Tool 名称。

无自由文本理由，避免把隐藏推理、凭据或任务正文写入观察证据。

## Authority 与动态撤权

持久 Contract 的安全位永久为：

- `provider_call_authorized=false`；
- `observation_recorded=false`；
- `production_model_visible=false`；
- `candidate_execution_authorized=false`；
- `side_effects_allowed=false`；
- `activation_authorized=false`。

实时 View 状态：

- `ready`：descriptor、baseline、model/reasoning 和 samples 全部 exact；
- `descriptor_revoked`：4a lease/source 已失效或 descriptor identity 变化；
- `catalog_changed`：生产 Tool Registry 已变化；
- `model_changed`：模型 identity/capability/rate/reasoning 已变化；
- `sample_changed`：Specification、interaction 或 scenario 已变化；
- `missing`：尚未形成契约。

只有 `ready` View 可以声明 `observation_input_eligible=true`，但这仍不是 provider-call authority。后续 observer
必须拥有独立预算与调用授权，并在调用前后重新读取本 View。

## Store 与并发

Store 以 `(workspace_root, binding_sha256)` 唯一绑定 exact descriptor/baseline/samples/model/sampling。两个独立
Store 并发编译相同 binding 时采用 `BEGIN IMMEDIATE` 和 first-wins，只返回同一 durable Contract。Contract
本体和 Store 外层摘要均校验；即使攻击者重算外层 SHA，内部 content identity 不一致仍 fail closed。读取与
first-wins 返回前还会逐项对账 `contract_id/candidate_id/descriptor_id/binding_sha256/created_at` 裁决列，禁止
关系字段与合法 payload 脱钩。

## 双通道与界面

- Agent Tool：`evolution_capability_shadow_observation_contract`，支持 `inspect/compile`，compile 可选模型；
- CLI/Textual TUI：`/evolution capability-shadow-observation <candidate-id> [model]` 与
  `capability-shadow-observation-status`；
- New UI：同名 typed action，经 Agent Tool 的统一权限入口；
- 回执明确显示 baseline 数量、正/负样本、模型 identity、reasoning、token/时间/成本预算和全部未授权位。

## 验收证据

- [x] current 4a View、Specification、真实 Registry 与真实 ModelCapabilityContract 形成 content-addressed 契约；
- [x] 正样本来自 interaction-backed scenarios，负控制来自真实 baseline descriptions；
- [x] fallback 模型关键参数、未知 reasoning auto、缺少 Tool/structured output 能力均 fail closed；
- [x] descriptor、catalog、model/reasoning 与 sample 任一漂移均动态撤权；
- [x] 跨 Store 并发 first-wins，内外层及关系裁决字段 tamper 均 fail closed；
- [x] Agent Tool、CLI/TUI 与 New UI typed action 走同一 Service；
- [x] Python/Node 小模块测试、Ruff、py_compile 和文档治理通过；未运行全量测试。

## 下一步

EVO-06.4c1 应实现 bounded Shadow Observation Runner：消费实时 `ready` Contract，取得独立 provider-call
permission 与预算 reservation，逐样本调用但绝不执行返回的 tool call，只接受上述 structured schema，并形成
usage/provider evidence 可撤权 Observation Receipt。EVO-06.4c2 再聚合 precision/recall、false-positive、
indeterminate、成本与重复稳定性；任何门槛缺失都不得进入 EVO-06.5 Limited Activation。
