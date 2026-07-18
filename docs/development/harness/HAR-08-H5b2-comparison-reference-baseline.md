# HAR-08 H5b2 Comparison Reference Baseline

## 1. 目标

让 H5c 能比较“预期失败的 RED cohort”和修复后的 Candidate，同时不破坏 H5b 的全绿晋升规则。
普通 Baseline promotion 仍要求全绿并显式切换 selector；comparison reference 只提供不可变的比较外键，
绝不成为 active Baseline。

该能力是 EVO-03 RED→GREEN 数值判定的最小 Harness 前置，不建立第二套 Result、统计或 receipt 协议。

## 2. Typed purpose 与 schema v16

`harness_eval_baselines` 新增受约束字段：

- `promotion`：既有 H5b 全绿 Baseline，可由显式治理动作切换 selector；
- `comparison_reference`：允许业务失败态作为比较参考，但不写 selector 和 promotion event。

历史数据库升级时原有记录自动标记为 `promotion`，不重写原摘要。新 reference 的 baseline digest 额外绑定
`purpose=comparison_reference`，因此手工篡改 purpose 会在读取时被检测。

## 3. Reference gate

`HarnessStore.register_eval_comparison_reference()` 要求：

- cohort 非空且 `sample_index` 从 0 连续；
- 全部样本具有相同、非空、可验证的 Identity；
- Identity repetitions 与实际样本数相同，且仍通过 profile/source eligibility；
- Suite 至少有一个 case；
- 允许 `passed` 或 `implementation_failure`，从而表达真实 RED；
- 拒绝 `evaluation_error`、`skipped` 和任何未通过/未验证 guardrail；
- ordered sample-set digest 由 H5a 持久化 Result digest 计算。

同 workspace/suite/batch 的相同重试返回首次 reference；不同 cohort 不可覆盖。注册过程不创建 selector、
不创建 promotion event，也不把 RED 的业务失败误称为晋升成功。

## 4. H5c 复用

Reference 与普通 promotion 共用 H5b immutable ID、identity/sample digest 和 H5c 外键。H5c 仍从 H5a
重新读取两组样本，执行相同机械比较、Policy 和统计判定，并持久化同一种
`HarnessEvalComparisonReceipt`。上层不能根据 reference purpose 改写 verdict。

## 5. 验收证据

- 五次 `implementation_failure` RED 被普通 promotion 的全绿 gate 拒绝；
- 相同 RED 可注册为 `comparison_reference`，幂等重试保留首次审计字段；
- active selector 和 promotion event 均保持空；
- 五次全绿 GREEN 与 reference 生成 H5c，统计 verdict 为 `improved`、decision 为 `passed`；
- 含 `evaluation_error` 的 RED 被 reference gate 拒绝；
- schema v15 数据升级为 v16 后保持 `purpose=promotion` 和原摘要可读。

## 6. 当前边界

- 这是内部 Store authority，不新增 Slash/Agent Tool，避免用户绕过显式 Baseline promotion；
- reference 会出现在完整 Baseline version 列表中，消费者必须读取 `purpose` 区分，不能展示为“已晋升”；
- 下一切片由 EVO-03.4a 校验 RED/GREEN completion receipts 后调用本 gate和 H5c，不允许任意失败 batch
  注册为自进化比较依据。
