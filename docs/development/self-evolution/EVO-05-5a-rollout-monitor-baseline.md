# EVO-05.5a Rollout Monitor Baseline

## 目标

在 runtime monitor 计算“回归”之前冻结可信比较基线。基线不能来自配置默认值、当前 canary 自身、模型叙述或缺失数据补零；
它必须来自 Rollout Plan 绑定的 Fresh Final Evaluation 及其 exact Interventional GREEN H5a cohort。

## 证据链

`EvolutionRevalidationRolloutBaselineService` 动态重读：

1. current immutable Rollout Plan；
2. Plan 绑定的 Fresh Final Evaluation；
3. Final 中 exact Interventional cohort receipt；
4. cohort 的 ordered GREEN batch/result digests；
5. Harness Store 中 sample index 连续的原始 H5a suite results。

任一 ID、digest、样本顺序、数量或 raw H5a 缺失都会 fail closed。已持久化 Baseline 保留审计，但动态失去
`monitor_input_ready`。

## 冻结指标

- 每个 GREEN sample 的 suite duration，转换为整数 microseconds；
- nearest-rank p95 duration；
- passed/failed run count；
- completion/error rate basis points；
- 每个 sample 的 integer micro-USD 与 mean cost；
- exact GREEN result digest tuple、batch、suite、cohort、Final、Contract 与 Plan identity。

Interventional identity 明确 `live=false` 且所有 case 无 Live Evidence 时，Provider/模型成本可证明为零，来源标记为
`no_model_execution`。`live=true` 时必须存在 Live Evidence，且每项 cost source 不得为 unavailable；否则拒绝生成基线。
这避免把“没有成本数据”错误解释成“成本为零”。

Baseline 只提供 monitor input authority，不授予 stage advance、rollback 或 promotion。

## 验收结果

- 8 个独立 Service 并发签发得到同一个 content-addressed Baseline；
- exact GREEN H5a 样本数、顺序与 digest 全部重验；
- duration、p95、completion/error basis points 与 no-model zero cost 机械计算；
- 删除任一 raw GREEN H5a 后，历史 Baseline 动态变为不可用于 monitor；
- 删除 Final dependency 后，Store 拒绝重新持久化同一 Baseline；
- production Engine 与公共 lazy exports 已装配；
- 只运行相关小模块测试，未运行全量测试。

## 下一切片

[EVO-05.5b](EVO-05-5b-runtime-observation.md) 已将本 Baseline 与 EVO-05.4b2 terminal canary journal 比较，形成
`insufficient|passing|breached` observation receipt。它必须同时处理最小样本数、最短观察窗口、错误率、p95 latency、
completion-rate drop、cost regression、用户撤回和安全/数据完整性信号。passing 不直接进入下一阶段；breached 只开放
EVO-05.6a 的自动 pause/rollback 输入。
