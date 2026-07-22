# EVO-03.6e Adversarial RED/GREEN H5c Comparison

## 目标

严格配对同一 Adversarial Batch、同一平台的 RED/GREEN continuous cohort receipt，重新读取并验证两组
ordered H5a evidence，然后复用唯一 `EvolutionComparisonKernel` 形成原生 HAR-08 H5b2/H5c receipt。
本切片不建立 Evolution 私有评分器、不重新执行 check、不调度其他平台，也不做自动晋升。

## Authority Gate

`EvolutionAdversarialComparisonExecutor` 在任何 H5b2 写入前重新验证：

- Batch Request、Probe Contract、Validation Plan 的 digest、candidate、Profile、Lease 与 coverage 绑定；
- 两份 cohort receipt 来自同一 request/probe/plan/lease/candidate/suite/sample seed；
- lane 精确存在于请求矩阵，顺序、batch、phase 必须是同平台 RED 与 GREEN；
- RED source 是 clean baseline commit/tree，GREEN source 是同 commit 的 dirty candidate snapshot；
- 两组 H5a 均为连续 0..N-1、数量和 result digest 与 completion receipt 精确一致；
- configuration、platform、baseline identity 在各 cohort 内稳定且跨 lane 可比较；
- check 顺序、runner、probe kinds、metric 结构、lifecycle 与每 sample 单一 Run Grant 可机械重算；
- cohort check summary、sample value 与 Run Grant digest 集合等于 H5a 重算结果。

## 共享 H5b2/H5c

完成 lane-specific authority gate 后，只调用 `EvolutionComparisonKernel`：RED 注册为
`comparison_reference`，H5c 使用现有 mechanical/Policy/statistical comparator 与事务型 Store。
重复执行返回同一 reference 和 comparison receipt；冲突、损坏或不可比较证据使用稳定错误码失败。

## 验收证据

- 真实临时 Git workspace 执行当前平台 RED/GREEN 两条 lane；
- 10 个 H5a sample、20 次 Profile check 均通过 ARC-04 worker 与 HAR-08.4f coordinator；
- comparison 得到 `statistical=unchanged`、`decision=passed`，5 个 sample 均为 mechanical unchanged；
- 完整重试返回同一 H5c receipt；把 RED receipt 冒充 GREEN 在持久化前被拒绝；
- Engine composition 同时公开 6d cohort executor 和 6e comparison executor；lazy exports 可导入；
- 仅运行该真实垂直链路、composition、Ruff 与编译定向验证，不运行全量测试。

## 当前不足与下一步

当前只比较单个平台的一对 receipt。跨平台 dispatcher、matrix 汇总、通用 Harness Sandbox Eval
Service/Tool/UI、Failure Attribution adversarial adapter 与 EVO-03.7 最终 Evaluation Receipt 尚未完成。
下一选择应重新横向比较这些依赖，不继续扩张 comparison 内核。
