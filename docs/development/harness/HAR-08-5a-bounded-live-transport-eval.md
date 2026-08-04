# HAR-08.5a 有界 Live 模型传输评测

## 状态

已实现。该切片交付 HAR-08.5 的最小生产前置：在用户显式触发、权限策略允许且成本合同可信时，
向一个真实 Provider 模型发送一次固定、无工具、无工作区内容的挑战，并返回防篡改且不含原始模型输出的
typed receipt。

它不是完整 Live Eval Suite，也不会持久化为 H5a 样本或自动成为 Baseline。HAR-08 整体继续保持
`partial`。

## 目标

在扩大到声明式 Live Suite、重复样本和 Baseline 比较前，先证明以下生产边界成立：

1. Live 调用默认关闭，只能由显式 Slash 或 Agent Tool 发起。
2. 请求前取得真实模型能力合同、思考强度身份和可信价格来源。
3. 在 Provider 调用前计算保守成本上界；超过用户上限时不发送请求。
4. 单次调用同时受本地 deadline、最大输出 token 和成本上限约束。
5. 回执记录 Provider 实际返回的模型身份、完整 token 用量、成本和终止原因。
6. 固定挑战必须精确匹配，Provider/合同/用量异常不得被判为通过。
7. 不保存用户内容、工作区内容、reasoning 或模型原始输出，只保存响应 SHA-256。
8. CLI、New UI、Textual TUI 和 Agent Tool 共享一个 Service/Runner 权威。

## 非目标

- 不读取工作区文件，不运行 Tool，不启用并行工具调用。
- 不使用用户输入作为 Prompt，不执行 LLM Judge。
- 不重试 Provider 请求，避免一次用户动作产生不透明的多次计费。
- 不写入 H5a，不创建或切换 Baseline，不参与 H5c 比较。
- 不声称本地 timeout 能证明 Provider 已停止远端推理或计费。
- 不在本切片提供专用 typed Live Eval 页面、历史目录或跨平台 Provider CI 矩阵。

## 调用面

### 用户命令

```text
/harness eval live [--model <id>] [--timeout 1..120] [--max-cost 0..10] [--max-output 1..64]
```

省略模型时，由 `ModelPort.resolve_model("capable")` 选择当前 capable/default model。命令解析器拒绝
未知参数、重复参数、缺值和越界数值，然后通过 `AgentEngine.execute_tool()` 进入正常权限管线。

### Agent Tool

`harness_eval_live` 是非只读、非破坏性、可并发的显式外部调用 Tool：

- normal 模式需要一次确认；
- bypass 模式按全权限语义直通，不产生二次确认；
- 每个会话最多调用 10 次；
- 风险级别为 `medium`；
- Slash 与 Tool 最终都调用 `HarnessService.eval_live()`。

## 请求合同

`HarnessLiveEvalRequest` 使用严格、冻结、拒绝额外字段的 schema：

| 字段 | 约束 | 说明 |
| --- | --- | --- |
| `live` | 必须为 `true` | 防止普通 Eval 隐式升级为付费调用 |
| `model` | 1..512 字符，无控制字符 | 精确绑定请求模型 |
| `max_duration_seconds` | 1..120 | 本地等待上限 |
| `max_cost_usd` | `>0` 且 `<=10` | 单次成本预算 |
| `max_output_tokens` | 1..64 | 固定挑战无需大输出 |
| `runner_version` | `live_transport_echo@1` | Runner 可比较身份 |
| `prompt_version` | `naumi_live_echo@1` | 固定 Prompt 身份 |

请求以 canonical JSON 计算 SHA-256。布尔值不能伪装成整数或浮点预算，NaN/Infinity 和控制字符均被拒绝。

## 执行协议

### 1. 建立请求身份

Runner 生成 `hlive_<24 hex>` request id，并记录 canonical UTC 时间。请求 ID、时钟或模型合同异常时，
在 Provider 调用前失败关闭。

### 2. 请求前模型身份

Runner 通过共享 `ModelPort` 读取：

- capability contract：requested/canonical/upstream model、provider、API format、能力来源和状态；
- reasoning status：有效思考强度、来源、支持集合和默认值。

两者合成为 `HarnessEvalModelIdentity`。明确 `incompatible` 的模型不会被调用；输入或输出价格来源缺失、
为 fallback 或数值无效时，也不会发送请求。

### 3. 保守成本预检

挑战只包含固定 system 指令和随机 ASCII token。输入 token ceiling 使用完整 UTF-8 byte 数加 256 token
协议余量，再结合 `max_output_tokens` 和 capability contract 中的输入/输出单价计算最坏成本。

预估超过 `max_cost_usd` 时返回 `preflight_cost_exceeded`，Provider 调用次数必须为零。该算法刻意偏保守，
不会用未经验证的 tokenizer 精确值降低安全余量。

### 4. 单次真实调用

Runner 发送一个无 Tool、`temperature=0`、最大输出受限的请求：

```text
NAUMI_LIVE_OK_<request-id-suffix>
```

模型必须只返回该 token。调用不携带用户内容、工作区内容或历史会话，不执行重试。

### 5. 调用后复验

响应返回后重新读取 capability/reasoning identity。请求前后身份漂移时结果为 `partial`。随后机械验证：

- Provider 返回的实际模型名存在且只含安全字符；
- input/output/total token 均为有限非负值，且总量严格相加；
- Provider 报告的实际成本有限、非负且不超过用户上限；
- finish reason 为 `stop` 或 `end_turn`；
- 去除首尾空白后的内容与随机挑战精确匹配。

## 状态与错误分类

| 状态 | 典型 code | 含义 |
| --- | --- | --- |
| `passed` | `live_transport_verified` | 身份、用量、预算、终止与挑战均通过 |
| `implementation_failure` | `challenge_mismatch` | 请求完成，但被测模型未满足固定协议 |
| `evaluation_error` | `model_incompatible`、`cost_contract_unverified`、`provider_request_failed` | 评测环境或 Provider 调用本身不可用 |
| `partial` | `deadline_exceeded`、`usage_inconsistent`、`model_contract_drift`、`actual_cost_exceeded` | 已产生部分真实执行证据，但不能形成通过结论 |

Provider 私有异常不会写入回执或用户界面。取消任务时 `CancelledError` 原样传播，避免 Harness 吞掉上层
取消语义。

## 回执与隐私

`HarnessLiveEvalReceipt` 是严格冻结模型，并以排除自身摘要后的 canonical JSON 计算
`receipt_sha256`。重读时会复验：

- request id 和 UTC 时间格式；
- receipt SHA-256；
- total token 加和；
- passed 必须同时具备精确匹配、响应摘要、Provider 模型身份且未超预算；
- `persisted=false` 与 `baseline_eligible=false` 是 literal，不允许调用方改写。

回执只保存响应 SHA-256，不保存挑战明文或模型输出。终端 renderer 显示有限身份、用量、成本、耗时和
摘要前缀，不显示 reasoning 或 Provider 异常详情。

## 权威代码

- Runner/Request/Receipt：`src/naumi_agent/harness/eval_live.py`
- 共享 Service：`src/naumi_agent/harness/service.py`
- Agent Tool：`src/naumi_agent/harness/tools.py`
- Slash surface：`src/naumi_agent/main.py`
- 权限策略：`src/naumi_agent/safety/permissions.py`
- Provider 实际模型投影：`src/naumi_agent/model/router.py`
- 聚焦测试：`tests/unit/test_harness_eval_live.py`

## 验收证据

- 固定挑战成功时只形成摘要，不保留原始响应或 reasoning。
- 回执任一受保护字段被修改后无法通过 Pydantic 重读。
- incompatible、fallback price 与预估超支均在零 Provider 调用下失败关闭。
- timeout、用量不一致、挑战不匹配、身份漂移和实际超支均产生不同稳定分类。
- Service、Tool、Slash 和权限检查复用同一调用权威；normal 确认、bypass 直通。
- ModelRouter 投影 Provider 实际返回的模型，而不是只记录请求别名。
- 仅运行 HAR-08.5a 及其直接 surface/router/permission 聚焦测试，不以全量测试替代边界证据。

## 已知限制与下一切片

1. `asyncio.timeout` 只能终止本地等待并尝试取消协程，远端是否停止推理和计费仍需 Provider 账单或
   cancellation contract 证明。
2. 当前只验证 transport，不代表模型在工具使用、长上下文、结构化输出或复杂推理上的质量。
3. 单次结果不可用于统计比较；随机性 Live case 仍须至少 5 个同身份样本。
4. 当前回执不持久化，因此不能进入 H5a/H5b/H5c。
5. 专用 typed Live 页、历史查询和 macOS/Linux/Windows Provider matrix 尚未实现。

HAR-08.5b 已完成声明式 Live Suite/Case、可重复样本身份、逐样本 H5a 持久化和批次成本审计，详见
`HAR-08-5b-declarative-live-suite-batch.md`。下一步 8.5c 仍需补 Provider cancellation/billing 证明、专用
typed 进度/历史和三平台 Provider matrix；不得宣称完整 HAR-08.5 或 HAR-08 已完成。
