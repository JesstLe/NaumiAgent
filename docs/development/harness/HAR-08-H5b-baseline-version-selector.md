# HAR-08 H5b Baseline 版本、Selector 与晋升 Gate

## 1. 目标

把 H5a 的 immutable Eval cohort 显式晋升为不可覆盖的 Baseline 版本，并用单独的 active selector
指向当前版本。晋升是治理动作，不等同于“运行成功”，也不能绕过 Identity、样本完整性或 guardrail。

本切片不保存 before/after Comparison receipt，不开放 Slash/API/UI，也不自动选择历史最好结果。

## 2. Schema v9

- `harness_eval_baselines`：workspace/suite 内单调 version，引用 batch、Identity、sample count 与完整
  sample-set digest；保存 actor、reason、created_at 和整行 `baseline_sha256`。
- `harness_eval_baseline_selectors`：每个 workspace/suite 只有一个 active baseline ID；新版本晋升在同一
  SQLite 事务内原子更新，整行 selector digest 防止静默回拨或跨边界改写。
- `harness_eval_baseline_events`：每次首次晋升追加事件，保存 previous/current baseline、actor、reason、
  time 和 `event_sha256`。

Baseline ID 由 workspace/suite/batch 稳定生成；同 batch 重试幂等，不因新的 actor、reason 或时间改写
首次事实，也不会把已经切到 v2 的 selector 回拨到 v1。

## 3. 晋升 Eligibility

`promote_eval_baseline()` 在事务内读取精确 workspace/batch/suite cohort，并依次要求：

1. cohort 非空，sample index 从 0 连续递增；
2. 所有 sample 有同一个非空 Identity；
3. Identity `baseline_eligible=true` 且没有 `baseline_identity_code`；
4. Identity repetitions 等于实际样本数；
5. Suite Result 非空且全部 passed；
6. 每个 case passed；
7. 每项 guardrail passed，不接受 unverified；
8. sample-set digest 由有序 sample index/result digest 机械生成。

任一条件失败都不创建版本、不更新 selector、不写审计事件。

## 4. 版本与读取语义

- 首次合格 cohort 为 v1；后续不同 batch 在同 workspace/suite 下依次 v2、v3；
- active selector 永远指向最后一次成功的新版本；
- list 按 version 倒序，读取有 1..1000 上限；
- 同名 suite 在其他 workspace 不共享版本或 selector；
- selector JOIN 同时校验 baseline ID、workspace 和 suite，手工跨边界指针不会返回数据；
- Baseline 与 audit event 每次读取都复核内容摘要，SQLite 行被修改后返回 Store 损坏。

## 5. 已验证场景

- 真实 Git production hello Suite 运行、持久化五次样本后成功晋升 v1；
- 同一 cohort 重试返回 v1，保留首次 actor/reason/time；
- 第二 cohort 晋升 v2 并原子成为 active，版本列表为 2/1；
- v2 后重试 v1 不回拨 selector，也不追加重复事件；
- 审计事件形成 `空 → v1 → v2` 链；
- 无 Identity、sample 缺口、unverified guardrail 均被拒绝；
- Baseline 行或 audit actor 被篡改后读取失败；
- v1/v2/v3/v4/v7/v8 数据库 additive 迁移到 schema v9。

## 6. 后续

- H5c：不可变 Comparison receipt，引用 baseline/current batch 与全部 verdict digest；
- HAR-08.8：只读 list/detail/compare 与显式 promote surface；
- HAR-09/EVO-03：只能引用 Baseline ID 和 Comparison receipt，不能自行改 selector；
- HAR-06：定义 workspace 删除和 retention 对 sample/baseline/event 的保留策略。
