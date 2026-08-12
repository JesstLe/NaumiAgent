# EVO-06.3b1 Interaction-backed Executable Scenario Binding

## 目标

把 EVO-06.2b 中面向人阅读的 verification scenario 转换为可由下一阶段机械执行的输入与 oracle，
并把每一份 Binding 精确绑定到 EVO-06.3a sealed Artifact、完整 Specification 和一条 Harness 人工终态。

本切片只签发 `sandbox_execution_eligible`，不 materialize/import 候选源码、不调用 `execute()`、不修改
`ToolRegistry`，也不授予 Sandbox 执行、临时注册、Shadow 或生产执行 authority。真实隔离执行属于
EVO-06.3b2。

## 为什么 06.2b 的 prose fixture 不足以执行

06.2b 的 `fixture` 与 `expected` 是治理人员可读说明，不能可靠决定 Python 参数、JSON 类型、错误码和超时。
若直接让模型把 prose 临场解释为测试，重放结果会随模型、prompt 和上下文变化，也无法证明预期结果满足
接口 schema。因此 06.3b1 要求用户通过 Harness durable interaction 提交精确 JSON：

```json
{
  "scenarios": [{
    "name": "比较两份真实轨迹",
    "arguments": {"left": "data/traces/a.json"},
    "expectation": {"kind": "result", "value": {"differences": []}},
    "timeout_ms": 1500
  }]
}
```

`expectation.kind=error` 时只能声明 Specification interface 中已有的 `error_code`。结果 oracle 必须满足
`result_schema`，参数必须满足 `parameters_schema`；校验方言固定为 JSON Schema Draft 2020-12。

## 不可变 Binding

`EvolutionCapabilityScenarioBinding` 是 content-addressed、SQLite 持久化的不可变记录，包含：

- Candidate、Specification 与 sealed Artifact ID/digest；
- permission 与 verification specification digest；
- Harness interaction ID、sequence、digest 和 `answered_by=user`；
- 与规格同名、同顺序、完整覆盖的 1..8 个 executable scenarios；
- 每个 scenario 的精确 arguments、result/error oracle 与 100..300000ms timeout；
- 永久为 false 的 Sandbox execution authorization、Registry、Shadow 和 executable authority。

Store 对每个 Artifact 只接受首份 Binding；同一交互重复写入幂等，其他交互覆盖会被拒绝。canonical payload
和持久摘要在读取时同时校验。

## 动态撤权与恢复

- `inspect` 每次重读当前 Artifact，并重读 Harness source interaction；源码、规格、治理或人工交互失效时，
  历史 Binding 保留但 View 变为 `revoked`；
- Artifact 已失效时，不再把旧 pending interaction 显示成可继续的动作；
- 若 Harness 已提交人工答案但进程在 Binding 落库前崩溃，`advance` 从唯一未对账答案确定性恢复；
- 无效旧答案不会永久阻断修正后的下一次交互；尝试次数与 Harness 有界历史一致，单 Artifact 最多 100 次；
- 多份未对账答案、非人工 custom answer、subject/sequence/digest 不匹配、secret-like 输入或 schema 失败
  均 fail closed；
- bypass 不改变任何 lineage、schema 或 authority 检查。

## 双通道与 UI

- Slash：`/evolution capability-bind <candidate-id>`；
- Agent Tool：`evolution_capability_scenario_binding(action='inspect|advance', candidate_id=...)`；
- New UI：typed `evolution/review/request` 使用 `capability-bind`，Candidate detail 显示状态、场景名、oracle
  类型和 timeout；
- public payload 不传输 arguments、result oracle 或错误详情值；
- fallback/TUI 复用同一 Service 与 renderer。

## 验收证据

- 真实完整 Specification、人工 approved Governance、真实 sealed Python Artifact 和 Harness custom answer
  可形成 `ready` Binding；
- 空/错误参数、错误结果结构、未声明错误码、场景缺失/乱序、超限或疑似 secret 均被拒绝；
- answered-before-store 崩溃可恢复，store first-wins，payload 篡改读取失败；
- Artifact 漂移或 Harness source interaction 丢失会动态撤销 Sandbox execution eligibility；
- Slash、Agent Tool、New UI、Candidate detail 与 fallback 使用同一底层状态；
- Python 与 Node 相关小模块测试通过，不以 import smoke 或 mock-only 结果作为验收。

## 自我审视与下一步

本切片证明测试输入与 oracle 可机械解释，但尚未证明候选行为正确。
[EVO-06.3b2a](EVO-06-3b2a-content-addressed-sandbox-execution-request.md) 已先把 sealed Artifact、Binding、
exact Git source、overlays、argv、timeout、oracle digest 与 permission requirements 编译为不可变 Request，
且未签发执行权。EVO-06.3b2b 必须在 ARC-04/Harness Sandbox 中从该 Request materialize 临时模块，以隔离进程逐场景调用 `Tool.execute()`；对返回
字符串执行 JSON decode 后验证 result schema，或捕获
[ARC-01.3d1](../architecture/ARC-01-3d1-structured-tool-failure-contract.md) 结构化声明错误码，同时记录 timeout、资源、网络/文件
permission observation 与完整执行 Receipt。只有全部场景通过且来源仍 current，才能考虑短期、可撤销的临时
Registry lease；仍不得进入 Shadow 或 Limited Activation。
