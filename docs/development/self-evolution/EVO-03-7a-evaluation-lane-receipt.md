# EVO-03.7a Evaluation Lane Receipt

## 目标

把一条已经完成的 RED/GREEN H5a→H5c→Failure Attribution 链压缩为用户可读、防篡改、可持久化的
Evaluation Lane Receipt。它提供 before/after、样本状态、耗时、实际观测到的 token/cost、失败归因与完整
artifact digest 引用，但在模型层固定声明 `candidate_evaluation_complete=false`、`aggregation_required=true`。

因此本切片推进 EVO-03.7 的最终回执基础设施，同时禁止单平台或单 suite 证据冒充候选整体已验证。

## 数据合同

`EvolutionEvaluationLaneReceipt` schema v1 固定：

- lane kind：`self_review|interventional|adversarial`，由 RED/GREEN completion ID 前缀机械推导；
- workspace、platform、Validation Plan、candidate ID/revision 与 suite；
- H5c decision/statistical verdict/code；
- Failure Attribution category/reason/action 和 candidate/retry/rerun/reflection flags；
- RED/GREEN cohort 的 batch、identity、sample-set digest、sample/case 状态计数与总 duration；
- `tokens`、`usd` typed metric observations 的实际总量和 sample coverage；缺失时明确为 `null/0`，不估算；
- RED completion、GREEN completion、两组 sample set、H5c、Attribution 共六个有序 artifact 引用；
- 完整嵌入 typed H5c 与 Failure Attribution source receipts，并将全部投影字段与其交叉验证；
- evidence 时间范围、receipt identity/digest，以及不可变的非最终标志。

模型重新加载时验证六个 artifact 的类型、顺序、ID/digest 与一等字段完全一致，重新从 completion IDs 推导
lane kind，并让嵌入 receipt 自己重验 H5c decision 与 Attribution category/action 语义。修改这些投影必须同时
伪造嵌入的 source receipt 才可能通过；H5a 派生的 duration/resource 汇总则在每次签发时由 Store 重读验证。

## Authority 重读与持久化

`EvolutionEvaluationLaneReceiptExecutor` 不接受调用方拼装结果：

1. 通过工作区 + comparison ID 从 Harness Store 读取唯一 H5c；
2. 从共享 Failure Attribution Store 读取同 comparison 的不可变归因；
3. 重读 RED/GREEN H5a ordered cohorts，验证 batch/suite/index/count/identity/sample-set digest；
4. 再验证逐样本 GREEN digest 与 H5c evidence；
5. 从真实 H5a Result 汇总状态、duration 和 typed token/cost observations；
6. 生成 receipt 并以 comparison ID 为唯一键写入 `EvolutionEvaluationLaneReceiptStore`。

Store 对相同事实幂等，对同 comparison 的不同 receipt 冲突失败；读取时同时验证 JSON、自身摘要和 SQL 投影。
Harness 新增的 `get_eval_comparison_receipt_by_id()` 必须同时绑定 canonical workspace，禁止跨工作区枚举。

## 用户与 Agent 双通道

- 用户：`/evolution evaluation <comparison-id>`；
- Agent：`evolution_evaluation_receipt(comparison_id=...)`；
- 两者共用 Engine 中同一个 executor 和 `render_evaluation_lane_receipt()`；
- Slash 通过共享 router，因此 New UI、TUI 与兼容终端行为一致；权威 command index 同步展示新 subcommand；
- Tool 标记为非只读，因为首次签发会写入不可变派生事实，但不会运行项目代码或更改候选。

Renderer 在首屏明确显示“不是候选最终 Evaluation Receipt”，并展示 before/after、资源 coverage、归因和 authority。

## 权限治理补充

`evolution_evaluation_receipt` 现由 EVO-GOV-01 显式定为中风险派生写入：strict 可用、lockdown 阻断、
normal 无逐次确认、每会话最多 200 次；bypass 全权限但仍不能跳过 receipt authority 校验。详见
`EVO-GOV-01-agent-tool-permission-matrix.md`。

## 聚焦验收证据

- 在真实临时 workspace/Harness SQLite 中写入 5 个失败 RED 与 5 个通过 GREEN typed H5a；
- 注册真实 H5b2 reference，生成并持久化原生 H5c 与共享 Failure Attribution；
- Lane executor 按 ID 重新读取全链，得到 `interventional/linux`、`improved/passed`、`verified_improvement`；
- RED/GREEN 各 5 samples、各 50ms，实际 token 从 1000 降至 500、cost 从 2.5 降至 1.25；
- 伪造 H5c、伪造 Attribution、缺一个 GREEN sample、跨工作区查询均在写入前失败；
- 重复 executor、Slash、Agent Tool 与新 Store 实例返回同一 receipt/renderer；
- 篡改 SQL projection 后读取判定 Store 损坏；Engine composition 与既有 Failure Attribution 分类回归通过。

## 当前边界与后续依赖

本切片自身没有签发候选最终回执，也不会驱动 EVO-04。后续 EVO-03.7b1/3.7b2 已按 Validation Plan 与
Adversarial Batch Request 冻结并重读必要 lanes，只有 Interventional 与全部 required platforms 完整时才签发
Final Evaluation Receipt。跨平台 dispatcher 仍是默认三平台真实矩阵完成的外部前置。
