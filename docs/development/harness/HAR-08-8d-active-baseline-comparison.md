# HAR-08.8d Active Baseline Comparison

## 1. 目标

把 active Baseline 与一个完整 Candidate batch 通过 H5c builder 生成并持久化权威 Comparison receipt。
Service、Slash 和 Agent Tool 只负责编排 H5a/H5b/H5c，不重写机械、Policy 或统计规则。

本切片不晋升 Candidate、不根据比较结论自动切 selector、不把 passed 直接解释为可发布变更。

## 2. 共享入口

- 用户：`/harness baseline compare <suite> <candidate-batch>`；
- Agent：`harness_eval_compare(suite, candidate_batch_id)`；
- Service：`HarnessService.compare_eval_candidate()`；
- Builder/Store：复用 `build_eval_comparison_receipt()` 与
  `record_eval_comparison_receipt()`。

比较 Tool 会写 immutable receipt，因此不是 read-only；它不修改工作区、Baseline selector 或 Candidate
samples。相同 Baseline/Candidate 再次调用直接返回既有 receipt，不因新时间生成冲突回执。

## 3. 编排链路

1. 校验 Suite 和 Candidate batch ID；
2. 读取当前 workspace/suite 的 active Baseline；
3. 拒绝 Candidate 与 active Baseline 使用同一 batch；
4. 按 Baseline ID/Candidate batch 查找既有 receipt，存在则幂等返回；
5. 读取 Baseline 和 Candidate 两组 H5a samples；
6. Candidate 必须非空、sample index 连续、Identity 非空且统一、实际样本数等于 Identity repetitions；
7. 将 exact sample index/result digest/typed Result 交给 H5c builder；
8. H5c 执行逐样本机械/Policy 和全组统计聚合，Store 复核完整引用链后写入；
9. 写入后重新读取 active selector，检测比较期间的并发版本切换。

Identity/Suite/Policy 不兼容不是编排错误：H5c 会保存 `incompatible` receipt。缺失或不完整 cohort 则不
创建 receipt，因为它尚不具备声明完整重复比较的证据。

## 4. Typed 结果

`HarnessEvalComparisonRunStatus` 区分：

- `created`：已写入新的权威 receipt；
- `existing`：同一不可变比较已存在；
- `stale_baseline`：receipt 已正确引用并保存，但比较期间 active selector 已切到其他版本；
- `error`：缺少 Baseline/Candidate、Candidate 不完整、参数或 Store 失败，未写入 receipt。

成功显示中文 decision、Baseline version/batch、两组 sample count、统计 verdict、Policy 失败/证据不足
sample 数、短 receipt ID 与首次时间。`stale_baseline` 明确要求重新读取 active，而不删除仍然有效的历史
receipt。

## 5. 验收

- 真实 v1 active Baseline 与第二个五样本 Candidate 通过 Slash 产生 passed/unchanged receipt；
- Agent Tool 重试返回 existing，receipt 数量保持 1；
- 单样本不完整 Candidate 返回 `candidate_incomplete` 且不写 receipt；
- 无 active Baseline、缺失 Candidate、Candidate 等于 active batch 均明确拒绝；
- 注入 selector 在写入期间变化，返回 stale_baseline，receipt 仍引用原始真实 Baseline；
- Candidate 后续晋升 v2 后，v1 receipt 不混入 v2 active 状态页；
- H5a/H5b/H5c、Slash/Tool、Store 与 Bridge 定向测试保持通过。

## 6. 后续

- HAR-08.8e1 已完成 typed Baseline 状态页；8e2-8e4 继续完成 batch、promotion、comparison detail 与筛选；
- HAR-08.3/8.4/8.5：Replay、Sandbox、Live runner 复用同一 batch/receipt 协议；
- HAR-09：Proposal outcome 必须引用 receipt ID；
- EVO-03：验证阶段消费 receipt，不重新计算或让 LLM 覆盖 verdict。
