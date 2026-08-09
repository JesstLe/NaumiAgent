# EVO-05.5f4a Opt-in Runtime Liveness Window

## 目标

把 EVO-05.5f3 的 exact healthy probe、HAR-10.2i release binding 和 HAR-10.2j heartbeat observation ledger
机械合并为 content-addressed 的 opt-in runtime liveness window。本切片证明“明确 opt-in 的 exact candidate runtime 在冻结
时间段内持续报告存活”，不把 heartbeat 次数冒充真实 completed run，也不完成 opt-in stage。

## Exact 输入链

Builder 重新验证全部输入 artifact，并要求：

1. Runtime Health Receipt 为 healthy 且拥有单次 probe authority；
2. Health、Deployment、显式本机 cohort 和 release binding 属于同一 canonical workspace；
3. binding 的 pointer ID/digest/generation 与 activated pointer exact 一致；
4. slot、Boot Receipt、binary digest、version、target 与 Candidate Bundle Admission exact 一致；
5. 每个 sample 绑定同一 binding/runtime identity/subject/instance/epoch/timeout；
6. sample sequence 连续、前向 digest 连续，且首样本不早于 health completion；
7. assessed time 不早于末样本。

本机 `explicit_local_installation_opt_in` cohort 和 current deployment 是 exposure binding 的事实；它仍固定
`population_assignment_enforced=false`，不能冒充全局 1% population rollout。

## 机械窗口判定

冻结 opt-in stage 的 `minimum_observation_seconds`，并从 heartbeat timeout 推导：

`minimum_sample_count = ceil(minimum_observation_seconds / timeout) + 1`

只有末尾连续的 running/waiting 样本计入 operational sample；中途出现 starting/draining/stopped 会重置持续窗口，不能用
重置前的健康时长拼接出 passing。判定顺序为：

- 任一 `failed`、相邻 gap 超过 timeout、末样本 age 超过 timeout：`breached`；
- legacy origin、末状态非 running/waiting、样本数或持续时间不足：`insufficient`；
- 其余：`passing`。

最多冻结 5000 个样本；若当前 timeout 无法在该上限内证明计划窗口，明确失败而不是截断后虚报 passing。gap/age
使用精确时间差执行门禁，因此即使只超出 timeout 不足一秒也会 breach；公开的整数指标向上取整并保持有界。

## 权限边界

- passing 只开放 `runtime_liveness_window_authority`；
- breached 开放 pause/rollback input，后续执行者仍必须动态重验 deployment、control 与 ledger；
- 固定 `completed_runs_observed=0`、`completed_run_evidence_authority=false`；
- Stage Plan 的 `minimum_completed_runs` 只作为后续门槛冻结，heartbeat 绝不计作 completed run；
- stage completion、percentage、stable、promotion authority 全部固定 false。

## 验收结果

- 真实 candidate bundle → CAS deployment → health subprocess → runtime identity/binding → SQLite heartbeat ledger 全链形成
  passing liveness window；
- passing window 明确保留 required completed runs，但 completed-run authority 为 false；
- 短样本形成 insufficient，stale/gap/failed 形成机械 breach，正常 stopped 只形成 insufficient；
- timeout 只超出不足一秒仍形成 breach，draining 后恢复的短 suffix 不能拼接重置前时长；
- legacy snapshot origin 不能形成 passing；
- 样本链跳号/摘要断裂、错误 binding 组合和 completed-run authority 篡改均被拒绝；
- 公共 lazy export、ruff 与真实小模块测试通过；未运行全量测试。

## 后续交付

[EVO-05.5f4b](EVO-05-5f4b-durable-opt-in-liveness-assessment.md) 已把本合同接入 durable Store/Service，完成 HAR ledger
有界分页、并发收敛以及 receipt 后新增 gap/failure/control/pointer 漂移的动态失效。下一步仍需 release-bound 的真实用户
execution outcome ledger，才能满足 `minimum_completed_runs` 并形成独立 opt-in Stage Completion Evidence。
