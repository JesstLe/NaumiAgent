# EVO-03.7b2 Final Evaluation Receipt

## 目标

EVO-03.7a 只能证明一条 Evaluation lane 完整，EVO-03.7b1 只能冻结“完整集合应当包含什么”。本切片把两者
闭合：从 durable Store 重读 Aggregation Contract、一个 Interventional lane、合同要求的全部 Adversarial
platform lane，以及每条 Adversarial lane 的 RED/GREEN completion receipt，签发候选级 Final Evaluation
Receipt。

该回执只证明评测证据集合完整且可供机械决策，不接受 Candidate、不批准 promotion，也不允许 LLM 用叙事
覆盖失败、重跑或资源证据。

## Authority 链

执行器不信任调用方传入的对象，只接收一个 Contract ID、一个 Interventional H5c comparison ID 和 1..3 个
Adversarial H5c comparison ID，然后重新读取并验证：

1. Aggregation Contract 内嵌的 Batch Request、Validation Plan、Candidate revision、suite、sample 数和
   required platforms；
2. Interventional lane 的 workspace、Plan、Candidate、origin platform、H5a/H5c/Attribution 与 artifact
   digests；
3. 每个 required platform 恰好一条 Adversarial lane，且 RED/GREEN batch、lane order、sample 数与合同完全
   相同；
4. lane 引用的 RED/GREEN completion id/digest 必须能从 durable Store 重读；
5. completion 内的 request、platform、phase、batch、Plan、Candidate 和 ordered sample result digests 必须
   与 Batch Request 及 H5c lane 完全一致；
6. 所有 lane receipt、comparison receipt 不得重复，平台集合不得缺失或多出。

任何缺 lane、错平台、错 phase、换 batch、换 Candidate、换 Plan、completion 缺失或 digest 漂移都失败关闭。

## Durable completion authority

此前 Adversarial Cohort Executor 只返回 completion receipt，后续聚合无法独立证明 lane 引用的原始
completion。现在每次 cohort 完成并完成最终 authority 复验后，都会写入
`evolution_adversarial_cohort_receipts`：

- receipt identity 和 payload 不可变；
- request/request digest/platform/phase 唯一定位一条完成 lane；
- 重放返回同一 typed receipt；
- row columns、typed payload 与 receipt digest 任一不一致即拒绝读取；
- Store 只保存有界结构化证据，不保存源码、Prompt、凭据或任意执行输出。

Final receipt 写入 `evolution_final_evaluation_receipts`，以 Aggregation Contract ID 为不可变主键。并发重复
签发由 SQLite transaction 串行化；相同合同的不同最终内容拒绝覆盖。

## 最终回执语义

回执完整嵌入 Contract、Interventional lane、按合同顺序排列的 Adversarial lane 与 RED/GREEN completion，
并机械派生：

- RED/GREEN 总 sample、duration、token/cost observed coverage；
- comparison ids、failure categories 和 required actions；
- `any_candidate_fault`、`any_requires_rerun`、`all_reflection_eligible`；
- 整个证据集合的 first/last timestamp；
- canonical JSON + SHA-256 的 `evfinal_*` identity。

模型将以下边界写死：

- `candidate_evaluation_complete=true`；
- `aggregation_required=false`；
- `mechanical_gate_input_ready=true`；
- `candidate_acceptance_decided=false`；
- `promotion_ready=false`。

因此 EVO-04 可以把本回执作为完整输入，但仍必须由独立 mechanical gate 产生 accept/revise/reject/escalate。

## 双通道

- 用户：`/evolution evaluation-final <contract-id> <interventional-h5c-id> <adversarial-h5c-id...>`；
- Agent：`evolution_final_evaluation_receipt(...)`。

两条通道复用同一个 Executor、Builder、Store 和 renderer。默认 New UI 与 Textual fallback 都能通过共享 slash
协议显示回执；专用 typed 全屏页不是本切片内容。

## 聚焦验证

- 从真实 Git workspace、可信 Harness Profile、Validation Plan、Probe Contract 和 Batch Request 建立当前平台
  合同；
- 写入真实 H5a/H5c/Failure Attribution lane authority，再重读全部 Store 签发最终回执；
- 并发重复签发四次仍得到同一 artifact；
- Agent Tool 与 slash command 输出同一回执语义；
- 缺少 required platform、模型字段篡改与 SQLite column 篡改均失败关闭；
- 现有真实 shell/Harness Adversarial RED/GREEN cohort 各执行五个样本，completion receipt 可从新 Store 重读；
- Engine composition 验证所有 Store/Builder/Executor 使用同一 Session SQLite authority。

## 明确未完成

- 默认 Probe Contract 要求 Linux/macOS/Windows 时，跨平台 dispatcher 仍须实际收集三平台回执；本切片不会
  伪造缺失平台，也不会因当前机器只有一个平台而降低合同覆盖要求。
- A4 的真实 NaumiAgent 小模块 macOS/Linux 对照尚未完成，因此 EVO-03 整体保持 `partial`。
- EVO-04.1a Decision Input 至 4.7a Reflection Memory 已实现。
- Final Evaluation Receipt 的专用 New UI/TUI typed 页面与 golden 可在 UI-17 后续切片实现；当前共享 slash
  renderer 已可用，但不冒充专用交互页。

## 权限治理补充

`evolution_final_evaluation_receipt` 现由 EVO-GOV-01 显式定为中风险派生写入：strict 可用、lockdown
阻断、normal 无逐次确认、每会话最多 50 次；bypass 仍不能伪造缺失平台或把 receipt 解释为 Candidate
acceptance。详见 `EVO-GOV-01-agent-tool-permission-matrix.md`。

## 下一步

EVO-04.1a 至 4.7a 已补齐完整证据、四态 Decision、持久 Resolution 与非注入 Reflection。下一步实现
EVO-05.1a Promotion Package Input Contract；仍不执行 promotion。
