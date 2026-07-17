# HAR-08.8c 显式 Baseline 晋升

## 1. 目标

把 H5b 已实现的版本/selector/审计 gate 接入正常用户与 Agent 工作流。晋升是明确治理动作：调用方必须
指定 Suite、Candidate batch 和有意义的 reason；运行成功本身不会自动晋升。

本切片不自动生成 Comparison receipt，不根据历史指标自动选择“最好版本”，不允许旧版本重试回拨
active selector。

## 2. 共享入口

- 用户：`/harness baseline promote <suite> <batch> --reason <原因>`；
- Agent：`harness_eval_baseline_promote(suite, batch_id, reason)`；
- Service：`HarnessService.promote_eval_baseline()`；
- Store：复用 H5b `promote_eval_baseline()`，不建立第二套版本表或 selector。

Slash 的 actor 固定为 `user`，Agent Tool 固定为 `agent`，调用方不能伪造操作者。Tool 是非 read-only、
concurrency-safe；在 bypass 模式按全权限策略直接执行，其他模式沿用统一权限系统，不增加第二次高风险确认。

## 3. 前置校验与 Eligibility

Service 在访问 Store 前校验：

- Suite ID 非空且最多 64 字符；
- batch ID 符合 H5a 安全格式；
- reason 去除首尾空白后为 3..2000 字符；
- actor 只能是固定入口提供的 `user` 或 `agent`；
- batch 在当前 workspace/suite 下至少存在一个 sample。

真正晋升继续由 H5b 事务 gate 决定，要求 sample index 连续、Identity 唯一、repetitions 匹配、
`baseline_eligible=true`、Suite/Case 全绿且 guardrail 全部 passed。任一失败都不写版本、selector 或事件。

## 4. 结果语义

`HarnessEvalPromotionStatus` 明确区分：

- `promoted`：创建新版本并把 selector 原子切到该版本；
- `already_active`：同 batch 幂等重试，保留首次 actor/reason/time；
- `not_selected`：该 batch 已是历史版本，但当前 active 已更新；重试不回拨；
- `error`：缺失 cohort、eligibility 拒绝、Store 故障或 selector 异常，selector 未改变。

成功回执显示版本、样本数、短 Baseline ID、actor、首次 reason/time 和 previous baseline；错误回执显示稳定
code、中文原因与“Selector 未改变”。

## 5. 验收

- 真实 Git production hello Suite 在受信 Profile 下重复 5 次，Slash 晋升为 v1；
- 第二个真实五样本 batch 由 Agent Tool 晋升为 v2；
- active status 从 v1 变为 v2，事件 actor 为 `user/agent`，previous/current 链正确；
- 同一 active batch 重试返回 already_active，不追加版本或覆盖首次审计事实；
- v2 后重试 v1 返回 not_selected，selector 保持 v2；
- 未受信 Profile 生成的完整 batch 被 H5b eligibility gate 拒绝；
- 缺失 batch、非法 batch、短 reason 和错误参数不改变 selector；
- H5b/H5c Store、重复 batch、Slash/Tool 与 Bridge 相关定向测试保持通过。

## 6. 后续

- HAR-08.8d 已完成：将 Candidate 与 active Baseline 组合成 H5c Comparison receipt；
- HAR-08.8e3 已完成：typed New UI/TUI 引导理由、最终确认与结果页；receipt drill-down 由 8e4 继续；
- HAR-09/EVO-03：只能引用 Baseline ID 和 Comparison receipt，不得直接操作 selector；
- ARC-05/HAR-06：把 Baseline selector、事件与 receipt 纳入备份、迁移和 retention 协调。
