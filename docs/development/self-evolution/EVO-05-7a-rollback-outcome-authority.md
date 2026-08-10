# EVO-05.7a Rollback Outcome Authority

## 状态

已实现。

## 目标

把 [EVO-05.6b2a](EVO-05-6b2a-fenced-slot-rollback.md) 的真实、可验证 installed-slot rollback
Receipt 绑定回最初触发自进化的 Workbench Proposal，形成不可变 `rolled_back` Outcome。该 Outcome 是历史事实，
不是新的 promotion、policy learning 或长期指标 authority。

## 证据链

Outcome 必须动态重验同一条完整链路：

1. Rollback Execution Receipt、Request 与 Rollout Plan；
2. exact Fresh Promotion Input 及其内嵌的原始 Promotion Package Input；
3. 原始 Experiment Contract Authority；
4. Contract Source 中的 session、Workbench Proposal、deterministic Proposal Preview 与 Candidate revision；
5. candidate/baseline installed slot、rollback pointer history、Boot Receipt 与 Launch Resolution。

任一 JSON、row digest、Proposal binding、Contract Authority 或 rollback history 漂移，historical artifact 仍保留，
但 `outcome_authority=false`。不得仅凭 `candidate_id` 猜测 Proposal，也不得把旧
`EvolutionRevalidationOutcome`（旧 evidence 失效结果）复用为实施后的 Proposal Outcome。

## 持久化与并发

- 新表 `evolution_revalidation_rollback_outcomes` 以 `request_id` 和 `rollback_receipt_id` 双唯一约束单飞；
- Store 使用 `BEGIN IMMEDIATE` 与参数化 SQL，在同一事务内重验 Receipt、Request、Plan、Fresh/Prior Input 及
  完整 Experiment Contract Authority JSON；
- 8 路并发重复记录收敛到同一个 content-addressed Outcome；不同内容冲突失败关闭；
- 落盘后 Service 再次动态重验，source 已变化时不返回 authority；
- Request ID 在 Tool/Slash 边界和 Service 再验证，不接受路径、控制字符或自由文本。

## 产品入口

- Agent Tool：`evolution_revalidation_rollback_outcome`；
- Slash：`/evolution revalidation-rollback-outcome <rollback-request-id>`；
- Agent Tool、CLI、New UI 与 TUI 复用同一个 Service 和 Markdown Receipt；
- 该动作只写 durable evidence，normal 无逐次确认，bypass 同样直接执行，lockdown 阻断；
- 输出明确显示 Proposal、Experiment Contract、Candidate、breach 以及长期指标尚未记录。

## 权限边界

Outcome 固定：

- `status=rolled_back`、`outcome_recorded=true`；
- `promoted=false`、`superseded=false`；
- `long_term_metrics_recorded=false`、`learning_authority=false`；
- `promotion_authority=false`。

它证明 Proposal 对应的 candidate rollout 已发生可验证回滚，但不证明 rollback 后长期改善，不允许 HAR-09.6
标记 `promoted`，也不允许 EVO-06 自动学习。

## 验收结果

- 真实临时 Git baseline/candidate、immutable Experiment Contract Authority、Fresh/Prior Promotion Input、安装版本槽、
  boot probe、pointer CAS、Launch Resolution 与 Proposal lineage 全链路完成；
- 8 路并发 Outcome 记录收敛到同一 artifact；
- Agent Tool 与共享 Slash Router 显示同一 Outcome identity；
- Contract row 篡改后 Outcome 动态撤权；非法 Request ID 在访问存储前被拒绝；
- 相关小模块测试、Ruff、编译、public export、Engine composition、YAML 注册表检查通过；未运行全量测试。

## 当前边界与下一步

本切片只完成 `rolled_back` terminal fact。EVO-05.7 后续仍需：

1. promotion/deployment 成功后的 `promoted` Outcome；
2. 新 rollout 替代旧 Outcome 的显式 `superseded` ledger；
3. 回滚后恢复评测与长期观察窗口；
4. HAR-09.6c/6d 将 post-rollback verification 和长期指标回注 Workbench；
5. ARC-07.6 配置/数据恢复分支。

`HAR-09.6a Proposal Outcome Projection` 已完成：它只读消费本 Outcome，在不改写 `approved` 治理记录的
前提下显示 `rolled_back`，并在服务端禁止再次签发 Contract。`HAR-09.6b Before/After Outcome Evidence`
现已把原 Final Evaluation 的 RED/GREEN 实施比较绑定回 Proposal；它不是回滚后恢复或长期结果。下一最小切片
是 HAR-09.6c Post-Rollback Verification；长期指标缺失时仍禁止 `promoted` 与 policy learning。
