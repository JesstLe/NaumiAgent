# EVO-05.4a Immutable Staged Rollout Plan

## 目标

把动态 current 的 approved Fresh Decision 转换成不可变、内容寻址、可监控且默认可自动回滚的 staged rollout 计划。
计划只定义执行约束，不写 Git、不发布、不进入任何 stage；EVO-05.4b 才能在独立执行权限下启动 local canary。

## 权威输入

`EvolutionRevalidationRolloutPlanService.issue()` 必须动态复验：

1. Fresh Decision 的 Requirement、Response、专业签名、Principal key 与 technical gates 仍 current；
2. Decision status 为 `approved` 且 `current_staged_rollout_eligible=true`；
3. Decision exact 绑定 current Fresh Promotion Input；
4. Promotion Input 携带同一 Candidate revision、Runtime Contract、Fresh Final、target branch/head/tree；
5. prior immutable input 中的 patch manifest、migration assessment、rollback plan 与当前 Input digest 一致。

Service 在构造后再次检查 Decision，Store 在 `BEGIN IMMEDIATE` 事务中重读 typed Decision/Input，阻断审批失效或依赖竞态。

## 固定阶段 DAG

每份计划恰有四个阶段：

1. `local_canary`：0% 用户曝光，仅 synthetic/local workload；
2. `opt_in`：1% 等价的显式 opt-in channel；
3. `percentage`：按风险固定为 low 25%、medium 10%、high 5%、critical 1%；
4. `stable`：100% 目标状态，但必须显式进入，不能因计划签发自动到达。

每个阶段冻结 minimum observation、minimum completed runs、error rate、p95 latency regression、completion-rate drop、cost regression
阈值，并强制 `automatic_pause_on_breach=true`、`automatic_rollback_on_breach=true`。风险越高，曝光越小、观察期和样本越长、阈值越严格。
high/critical 或需要 data backup 的变更，每次推进都要求人工门；stable 永远要求人工推进。

## 权限边界

Plan 明确区分：

- `rollout_plan_authority=true`：证明计划可作为后续 stage-entry 输入；
- `stage_entry_authority=false`：尚未取得某阶段进入权；
- `execution_authority=false`：不能执行 patch、Git、merge、push 或 publish；
- `monitor_required=true` 与 `rollback_required=true`：未来执行器不可绕过监控/回滚链。

历史 Plan 可审计；若专业 Principal 换钥、签名撤销、Requirement/target/technical authority 漂移，动态 inspect 立即显示不可 rollout，
不会删除或改写历史计划。

## 验收结果

- 使用全角色真实 Response 与 Ed25519 专业签名形成 approved Decision，再生成四阶段 immutable Plan；
- 8 路并发签发得到同一 Plan；
- stage 阈值由 risk/migration 机械生成，Plan 模型拒绝任意改写；
- Principal 换钥后历史 Plan 动态 stale，next stage 清空；
- pending Decision 无法落盘 Plan；
- Plan 与既有 Fresh Decision 聚焦回归 4 项通过；未运行全量测试。

## 下一切片

EVO-05.4b 实现 local canary 的隔离 materialization、stage-entry receipt、kill switch 与 crash-safe execution journal；它只能执行第一阶段，
不能直接进入 percentage/stable。随后 EVO-05.5 采集真实运行信号，EVO-05.6 根据本计划阈值自动 pause/rollback，EVO-05.7 才形成
accept/rollback Outcome 并回注下一轮 Candidate。
