# EVO-03.1a Validation Plan Core

## 目标

把 EVO-02 产生的不可变 Mutation Receipt v2 编译成确定性、防篡改、不可执行的 Validation Plan，固定
baseline RED 与 candidate GREEN 必须使用同一指标、fixture、seed 和环境身份，并为改动语言声明
lint、compile、unit、contract、smoke 验证要求。

本切片不执行命令、不创建 HAR-08 样本、不生成比较结论，也不赋予 Lease 执行权限。

## 为什么不直接运行测试

HAR-08.4 Sandbox Runner 仍明确依赖 ARC-04 隔离 worker。直接在 Evolution Planner 中调用 subprocess 会
形成第二套执行器，绕过 Profile trust、进程回收、资源预算和后续 sandbox authority。EVO-03.1a 先固定
runner 必须消费的输入契约，后续切片再把要求绑定到可信 Profile check 与 HAR-08 Runner。

## Authority 链

`EvolutionValidationPlanner.plan()` 同时验证：

- `EvolutionExperimentContract`；
- active `ExperimentWorktreeLease`；
- baseline `EvolutionExperimentSourceSnapshot`；
- validation-ready `EvolutionMutationReceipt v2`。

四者的 Contract manifest、Lease、Snapshot、Candidate revision/digest、baseline commit、文件 scope 和指标
必须完全一致。Receipt v1、missing Profile、inactive Lease、metric 或 scope 漂移全部 fail-closed。

## RED/GREEN 指标对

每个 Contract `allowed_check` 形成一个 `ValidationMetricPair`：

- baseline phase 固定为 `red`；
- candidate phase 固定为 `green`；
- metric、direction、target、verifier、procedure 完整继承 Contract；
- `same_fixture_required=true`、`same_seed_required=true`；
- 顺序必须连续且与 Contract 一致。

计划不会把“测试执行成功”误写成“指标改善”，最终产品结论必须由 HAR-08 Comparison Receipt 给出。

## 文件验证要求

Planner 只按安全相对路径后缀进行确定性分类：

| 文件类型 | 必须解析的检查类型 |
| --- | --- |
| Python | lint、compile、unit、contract |
| JavaScript | lint、unit、contract |
| TypeScript | lint、compile、unit、contract |
| Swift/Rust/Go | compile、unit、contract |
| Markdown/YAML/JSON/TOML | lint、contract |
| 其他 | contract、smoke |

这些是 runner binding 的机械要求，不是自由命令字符串。当前 `runner_binding_status=required` 且
`execution_ready=false`；后续只有可信 Profile/HAR-08 adapter 能把要求解析为实际执行。

## 环境与防篡改

Plan 保存 baseline commit/tree digest、Profile digest、experiment config digest、toolset digest、Contract
seed、Mutation Receipt identity 和 candidate files digest，不保存绝对 worktree 路径或源码。canonical JSON
SHA-256 同时生成 `validation_plan_sha256` 与 `evvplan_` identity；嵌套文件检查要求被改写也会失效。

EVO-03.2d 已将 Plan 升级为 v2：每个文件同时绑定 Mutation Receipt 的 modify/create operation、baseline
before digest 与 candidate after digest。历史 v1 保持只读兼容，但 operation=unknown 不能进入 RED/GREEN
executor。详见 `EVO-03-2d-validation-file-operation-binding.md`。

## 验收证据

- 真实 Git worktree 完成 Generation→Guard→Writer→Mutation Receipt 后生成确定性 Validation Plan；
- RED/GREEN 对使用相同 metric、fixture 与 seed；
- Python 改动机械要求 lint/compile/unit/contract；
- main workspace 不被 Planner 修改；
- metric、Profile、Lease 漂移及嵌套 requirement 篡改全部拒绝；
- Engine 组合 Planner，历史 Mutation Generation/Receipt 聚焦回归继续通过。

## 当前不足与下一步

- EVO-03.1b 已把 check kind 按 changed path 绑定到可信 Harness Profile 的唯一 check ID/spec/argv digest，
  并保持不可执行；详见 `EVO-03-1b-validation-profile-binding.md`；
- RED/GREEN Self-Review 静态 cohort 已实现，Comparison Receipt 尚未实现；
- Sandbox/真实命令执行仍等待 ARC-04 authority，不能由本模块自行放宽；
- 下一最小切片是 EVO-03.4a Self-Review Quantitative Comparison Receipt。
