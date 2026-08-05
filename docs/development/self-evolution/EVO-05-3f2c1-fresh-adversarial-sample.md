# EVO-05.3f2c1 Fresh Adversarial Sample Pair

## 1. 目标

让 Fresh Runtime Contract 冻结的 adversarial probe 第一次在指定真实平台执行。一个 sample 在同一平台依次运行
current-target RED 与 immutable-overlay GREEN，形成两份 H5a 和一份不可变 pair receipt。

本切片不声称平台 cohort、跨平台 matrix、Adversarial H5c、Final Evaluation 或 promotion 已完成。

## 2. Authority Gate

- Contract 必须 current/ready，platform 必须属于 `required_platforms`；
- 实际 Worker 平台必须与 lane platform 相同，禁止本机冒充其他系统；
- Runtime Source Pair 的 Plan、Snapshot、candidate、suite、seed、sample count 与 platform matrix 必须匹配 Contract；
- current Profile digest 必须匹配 Fresh Validation Plan；
- 每个 probe check 的 spec、argv、timeout、probe kinds 必须匹配 Contract binding；
- Contract coverage 的所有 check ID 必须由当前 Profile 唯一覆盖；
- 执行需要父权限对 `bash_run` 的真实委托和有效 Run Grant。

## 3. 真实执行与证据

RED/GREEN 通过共享 `HarnessSandboxEvalExecutionKernel` 进入 ARC-04 Worker。每个 probe case 保存 lifecycle、Run Grant、
run scope、probe kinds 与 `adversarial.<check>.exit_zero` typed metric。两侧共享同一次平台捕获、configuration 与 Grant，
但 source identity 必须不同。

Receipt 冻结 Contract/Plan/Source、平台 identity、sample seed、两侧 H5a/identity/check/lifecycle/grant digest，并明确
`cohort_complete=false`、`comparison_authority=false`、`promotion_authority=false`。

## 4. 验收标准

- 当前真实平台完成一对 RED/GREEN adversarial H5a；
- ARC-04 Worker 实际执行物化源码中的 Python AST 检查，不使用 mock 作为端到端证据；
- 错误平台、stale source/profile/check、非法 sample/scope、缺父授权与缺 H5a 均 fail closed；
- 重复调用复验已有 authority 并幂等返回；
- Engine composition 和 lazy exports 完整；
- 仅运行本模块及相邻测试，不运行全量测试。

## 5. 下一步

EVO-05.3f2c2 已以 platform-scoped cohort Run Grant 执行连续样本并支持前缀恢复；EVO-05.3f2c3 聚合所有 required
platform lane，缺少对应 Worker 时保持 pending 而不是伪造完成；随后生成每个平台 H5c 与 matrix receipt。
