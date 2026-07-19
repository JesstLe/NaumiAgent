# EVO-03.2e Interventional RED Check Sample

## 目标

把 EVO-03.2a 的一个 RED sample 从“不可执行请求”推进为真实项目代码证据：重新验证 Plan、Profile
Binding、Baseline Request 与 Metric Binding，在精确 Git baseline commit/tree 上依次运行受信任 Profile
checks，并将一个 typed `HarnessEvalSuiteResult` 写入既有 HAR-08 H5a Store。

本切片只执行一个 sample 的 Profile checks。它不循环完整 cohort、不执行 metric runner、不创建
GREEN request，也不生成 H5c Comparison Receipt。

## 权威链

执行前必须重新解析并交叉验证：

- `EvolutionValidationPlan`；
- `EvolutionValidationProfileBinding`；
- `EvolutionBaselineCohortRequest`；
- `EvolutionMetricRunnerBinding`；
- 当前 `.naumi/harness.yaml` 的 digest、用户信任状态和每项 check 的 spec/argv/timeout digest；
- 父权限回执的执行权限、`run_id` 与 `bash_run` 委托范围。

已有 H5a sample 只能在 Suite、Request、baseline source、Profile identity、check 顺序、runner identity 与
ARC-04 lifecycle receipt 摘要全部匹配时幂等复用；否则 fail-closed。

## 执行路径

1. 在源码工作区取得 sample 专属 `HarnessRunKind.RUNTIME` lease。
2. 由父权限回执签发最长 3600 秒、绑定 lease owner/epoch 的 Run Delegation Grant。
3. 对 Request 中每个有序 check 调用 HAR-08.4c `HarnessSandboxCheckRunner`。
4. Runner 从精确 Git commit/tree 物化短生命周期快照，不读取候选脏工作树作为 baseline。
5. 每项 check 经 `ShellWorkerAdmissionComposer` 形成 ARC-04 Worker、Tool lease、delegated receipt、
   ExecutionGrant 与 ToolJob，并在结束后释放。
6. 结果映射到既有 H5a Suite/Case；Worker lifecycle receipt digest 被纳入不可变 case 与 sample receipt。
7. 成功、失败、取消和异常终态都撤销 Run Grant 并释放 Runtime lease。

## 结果语义

- Profile check `passed` 映射为 `EvalCaseStatus.PASSED`；
- 命令正常执行但断言/检查失败映射为 `IMPLEMENTATION_FAILURE`；
- timeout、cancel、resource limit、stale、blocked 与基础设施错误映射为 `EVALUATION_ERROR`；
- `metrics_executed=false` 是强制事实，不能用 Profile check 通过冒充目标 metric 已改善；
- `project_code_executed=true` 只有每项结果同时具备 Worker job ID 与 lifecycle receipt digest 才能生成。

## 验收证据

- 真实临时 Git 仓库把当前工作树改为 candidate 内容，check 仍从绑定 baseline commit/tree 读取并通过；
- 真实 macOS sandbox 与 ARC-04 Shell Worker 完成非 PTY、禁网的 Python check；
- H5a sample 只写入一次，重复请求返回相同防篡改 receipt；
- receipt 包含 sample seed、H5a result digest、baseline identity 与每项 lifecycle receipt digest；
- 运行结束后 Runtime lease 为 released，Run Grant 为 revoked，Tool Worker/lease 由 composer 回收；
- Profile digest 漂移在取得权限/lease 前阻断且不产生 H5a 结果；
- 相关 Ruff 与 13 个 Shell Worker/新 executor 定向测试通过，不运行全量测试。

## 后续状态

- EVO-03.2f 已在 H5a 首次持久化前合并 ready `self_review_static` typed metric observations，并把公共执行器
  名称升级为 `EvolutionInterventionalRedSampleExecutor`；旧 check-only 名称保留为兼容别名。
- EVO-03.2g 已按连续 sample index 调度并生成 cohort completion receipt；本切片自身仍只定义单 sample
  Profile-check 执行边界。
- candidate interventional lane 必须复用相同 seed/order/Profile/平台和资源合同，不能直接复用静态 GREEN
  executor 冒充项目代码验证。
