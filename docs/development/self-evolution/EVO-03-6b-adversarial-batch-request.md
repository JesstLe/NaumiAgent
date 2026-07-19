# EVO-03.6b Adversarial Batch Request

## 目标

把 coverage-complete 的 EVO-03.6a Probe Contract 编译为 HAR-08.4e/4f 可消费的、仍不可执行的 RED/GREEN
平台矩阵请求。请求冻结 probes、checks、样本、预算、平台和 candidate authority；它不读取父权限、不签发
Run Grant、不运行项目代码，也不创建新的 Evolution worker。

## Authority 输入

`EvolutionAdversarialBatchRequestBuilder` 只接受并重新校验三个强类型输入：

1. `EvolutionExperimentContract`：提供 candidate/source、seed 和总时限；
2. `EvolutionValidationPlan`：提供 Lease、Snapshot、Mutation Receipt、baseline、candidate files、Profile、
   experiment config 与 toolset identity；
3. `EvolutionAdversarialProbeContract`：提供 Registry、Profile Binding、平台身份、ordered requirements、checks
   和精确 coverage。

三者的 Contract/Plan/candidate/Profile/SHA authority 必须一致。Probe Contract 必须
`coverage_complete=true`、`blockers=[]`、`execution_ready=false`；缺失或歧义的 probes 不能进入 Batch Request。

## RED/GREEN 平台矩阵

- phases 固定为 `red, green`，不允许调用方删掉 RED 或改变顺序；
- 任一 requirement 声明 `platform_scope=matrix` 时，required platforms 固定为排序后的
  `linux, macos, windows`；
- 无 matrix requirement 时，只使用 Probe Contract 捕获的已知当前平台；`unknown` 直接阻断；
- 每个 `(platform, phase)` 形成唯一 ordered lane 和稳定 batch ID；matrix probe 因此生成六条 lane；
- 所有 lanes 使用同一组由 Experiment seed + Probe Contract SHA 派生的 5..100 个唯一 sample seeds。

Request 同时保存按 `(path, kind, probe_id)` 排序的 probe cases，以及按 check ID 排序、携带精确 coverage 的
check cases。一个 check 覆盖多个 probe 时每个 sample 只计一次 check timeout，但所有 probe/check mapping 必须
一一吻合。argv 不进入 Request，只保存 spec/argv SHA。

## 最坏预算

预算不使用平均耗时或历史成功耗时：

```text
check_timeout_per_sample = sum(unique bound check timeout)
lane_budget = max(60, check_timeout_per_sample * requested_samples)
matrix_budget = lane_budget * platform_count * 2 phases
```

`lane_budget` 同时满足 HAR-08.4f 60..3600 秒边界；`matrix_budget` 必须不超过 Experiment
`max_duration_seconds`。因此完整但过慢的 Profile 会得到 `adversarial_duration_budget_exceeded`，不会产生一个
实际上无法兑现的请求。

## 防篡改与执行边界

Request 的 identity 覆盖所有 authority、probe/check/coverage、lanes、sample seeds 和预算字段。模型校验会重新
推导 seeds、suite/batch IDs、lane 笛卡尔积、coverage、timeout 和 budget；只重新计算外层 SHA 也无法让错误的
嵌套结构通过。

Request 固定：

- `network_access=false`、`dependency_installation=false`；
- Profile trust 和 candidate Snapshot 必须在执行前重验；
- 每个 Batch 需要 Run Grant、连续 sample indexes 与 H5a Store；
- RED/GREEN 需要 HAR-08 H5c comparison receipt；
- `request_ready=true` 但 `execution_ready=false`。

`project_code_execution_allowed=true` 只声明后续受治理 worker 的任务性质，不构成权限；实际执行仍必须由
HAR-08.4f 读取父权限、获取 Runtime lease 并签发可撤销 Run Grant。

## 验收证据

- 真实临时 Git 工作区完整走过 Mutation → Validation → trusted Profile → Probe Contract → Batch Request；
- UI Python matrix probe 稳定展开 Linux/macOS/Windows × RED/GREEN 六条 ordered lanes；
- 两个唯一 checks、两个 ordered probes、5 个唯一 seeds 和 600 秒全矩阵最坏预算机械一致；
- 相同 authority 得到相同 Request，序列化结果不泄漏 Profile argv；
- lane phase 篡改无法反序列化；
- incomplete Probe Contract、100 samples 超预算和 bool sample count 分别以稳定 code 阻断；
- Engine 和 `naumi_agent.evolution` 公共 surface 暴露 Builder；Ruff、编译与定向测试通过，未运行全量测试。

## 当前不足与下一步

EVO-03.6c 已实现单 lane/sample authority adapter：精确 RED revision 与 GREEN candidate overlay 可通过
HAR-08.4e 真实执行，并写入带 lifecycle、Run Grant 和 `exit_zero` observation 的 adversarial H5a。详见
`EVO-03-6c-adversarial-lane-sample.md`。尚未实现的是 HAR-08.4f 连续 sample 接线、RED/GREEN H5c、跨平台
dispatcher 和最终 Evaluation Receipt；不得在 Evolution 内复制 Batch coordinator。
