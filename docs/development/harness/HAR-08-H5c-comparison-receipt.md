# HAR-08 H5c 不可变 Comparison Receipt

## 1. 目标

把 H5a 的两组精确 Eval samples、H5b 的 Baseline 版本、HAR-08.7b 的机械变化、8.7c 的
Policy gate 与 8.7d 的重复样本统计合成为一份不可变、可复核的权威结论。HAR-08.8、HAR-09
和 EVO-03 后续只能消费这份 receipt，不允许 UI、Tool 或自进化流程各自重算并覆盖结论。

本切片不新增 Slash/API/UI，不自动晋升 Baseline，也不把 receipt 直接解释为自进化补丁可发布。

## 2. Typed Receipt

`HarnessEvalComparisonReceipt` 使用 frozen/extra-forbid Pydantic 契约，包含：

- workspace、Suite、Baseline ID、Baseline/Candidate batch；
- 两组 cohort 的 Identity、sample count 与有序 sample-set SHA-256；
- 统计 verdict/code；
- 每个 Candidate sample 的 Result digest、机械 verdict/code、Policy verdict/code 与 violation codes；
- 总体 decision、带时区创建时间、稳定 ID 与整份 receipt SHA-256。

稳定 ID 由 workspace/suite/baseline/current batch 生成。同一比较键只能对应一份事实；receipt digest
覆盖除自身摘要字段外的全部 typed 内容。反序列化时重新验证稳定 ID、sample 序号、证据数量、总体
decision 聚合和内容摘要。

## 3. 构建与聚合规则

`build_eval_comparison_receipt()` 只接受带 `sample_index + result_sha256 + typed Result` 的有序样本：

1. 每个 Result digest 必须与完整持久化 JSON 一致；
2. 两组 sample index 都必须从 0 连续递增；
3. 每组内部必须具有一个统一、非空 Identity；
4. Baseline sample-set digest 必须与已晋升版本提供的摘要一致；
5. 统计层比较两组全部样本；
6. Policy 层以 Baseline 首样本为同 cohort 参考，逐一评估每个 Candidate，不能用代表样本隐藏波动；
7. 每个 Candidate 的机械和 Policy 证据进入 receipt。

总体 decision 的优先级为：

1. 任一不兼容 → `incompatible`；
2. 任一证据不足/不稳定评测 → `inconclusive`；
3. 任一 Policy 明确失败 → `failed`；
4. Policy 均允许但组内状态波动 → `flaky`；
5. 其余 → `passed`。

机械或统计 `regressed` 不会绕过 Policy，也不会自动覆盖 Suite 已声明的容忍阈值：如果每个样本均通过
Policy，receipt 可为 passed，但仍保留 regressed 原始 verdict，供用户和后续治理查看。

## 4. Schema v10 与存储边界

`harness_eval_comparison_receipts` 使用稳定 ID 作为主键，并对
workspace/suite/baseline/current batch 建唯一约束。行中保存索引字段、decision、receipt digest、完整
typed JSON 和创建时间；Baseline 使用受限外键，不能引用不存在的版本。

`record_eval_comparison_receipt()` 在写入前事务内复核：

- Baseline 属于同 workspace/suite，且 batch、Identity、sample count、sample-set digest 与 receipt 一致；
- Candidate batch 的每一行都通过 H5a Result JSON/digest/Identity/immutable-key 防篡改校验；
- Candidate 数量、连续 index、Identity、sample-set digest 和逐样本 evidence digest 全部一致；
- 同键同摘要重试返回首次记录；同键不同摘要拒绝覆盖。

读取单条和列表均限定 workspace/suite，列表有 1..1000 上限；每次读取重新验证 typed receipt、行索引
字段和 digest。手工修改 decision、JSON、摘要或作用域后不会把数据送入上层。

## 5. 验收证据

- 稳定两组五次样本产生 `passed + statistical unchanged`；
- 严格 Policy 下稳定回归产生 `failed` 并保留 violation codes；
- 宽松 Policy 允许单次回归时，组内摇摆产生 `flaky` 而不是伪装 passed；
- Suite Identity 不兼容产生 `incompatible`；
- digest 篡改、sample 缺口、错误 decision 聚合均被 typed contract 拒绝；
- Store 的重启恢复、幂等重试、冲突拒绝、workspace 隔离和行篡改检测通过；
- schema v1-v9 additive 升级至 v10，既有数据不被改写；
- 真实临时 Git 仓库运行 production hello Suite 两组各五次，持久化、晋升 Baseline、生成 receipt，
  再由新 Store 实例恢复为同一权威结论。

## 6. 后续依赖

- HAR-08.8a-8.8d 已完成：只读状态、重复 batch、显式 promote 和 active Baseline compare；
- HAR-09：反馈候选必须引用 receipt ID，不能只提交自然语言“变好了”；
- EVO-03：验证阶段引用 Baseline ID + receipt ID，禁止 LLM 改写机械/Policy/统计 verdict；
- HAR-06：补 Eval sample、Baseline、event、receipt 的 workspace/session retention 与删除协调；
- Replay/Sandbox/Live runner 继续生成同一 receipt，不建立平行比较协议。
