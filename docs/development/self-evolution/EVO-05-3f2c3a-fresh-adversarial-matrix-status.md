# EVO-05.3f2c3a Fresh Adversarial Matrix Status

## 目标

为 Fresh Runtime Contract 的全部 `required_platforms` 建立 fail-closed 状态权威。该切片不执行远端任务，而是机械区分：

- `completed`：平台 cohort 已存在，且 workspace、Contract、Validation Plan、Source Snapshot、suite 与样本数完全匹配；
- `runnable`：cohort 尚不存在，但匹配平台的活动 Tool Worker 已携带可信实时心跳，通过协议、能力、隔离、运行时长、
  accepting-jobs 与容量准入；
- `pending`：既无可信 cohort，也无实时准入 Worker。

只有全部 lane 为 `completed` 时才签发并持久化 `matrix_complete=true`。不完整状态是随 Worker 心跳变化的实时观察，不能作为
完成回执持久化。

## 真实实现

- `EvolutionRevalidationAdversarialMatrixService` 每次先动态复验 Runtime Contract，stale/blocked Contract 立即失败；
- cohort 完成状态从持久化回执重建，并复验所有上游 identity/digest；
- Worker 可运行状态调用 Worker Registry 的活动 incarnation 与原生 admission kernel，不把“注册过”视为“健康可运行”；
- macOS Contract lane 与 Worker Contract 的规范值 `darwin` 做显式映射；
- 完成矩阵绑定 ordered platform lanes 与每个平台 cohort digest，重放时再次复验底层 cohort；
- 本切片的 `dispatch_authority`、`comparison_authority`、`promotion_authority` 均保持 `false`。

## 验收标准

- 无 Worker、无 cohort 的 required platform 必须为 `pending`；
- 缺失/过期/拒绝任务/容量耗尽/能力不足的 Worker 不得成为 `runnable`；
- 只有通过实时健康准入的匹配平台 Worker 才能形成 `runnable`，但不得获得 dispatch authority；
- 全部 required platform cohort 齐全前不得持久化 matrix completion；
- 完成矩阵必须可幂等重放，并在 Contract stale、cohort 缺失或 digest 变化时失败关闭；
- 定向 Ruff、模块测试与引擎 import 通过。

## 后续依赖

EVO-05.3f2c3b 将消费 `runnable` lane，建立带 Run Grant、Job/Worker fencing、容量 reservation 和结果回传的远端调度；
EVO-05.3f2c4 再为每个已完成平台从原始 H5a 生成 H5b2/H5c，并聚合跨平台判定。完成这些步骤前不得进入 failure
attribution、Final Evaluation 或 promotion。
