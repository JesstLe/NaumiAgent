# HAR-09.6c2a1 Post-Rollback Behavioral Coverage Contract

## 状态

已实现。

## 目标

在 HAR-09.6c2a 单 lane 能力与 HAR-09.6c2b 完整矩阵之间，建立不可变、可复核的覆盖契约。契约从同一
Proposal-bound `rolled_back` Outcome、HAR-09.6c1 Runtime Verification 与 HAR-09.6b Before/After Evidence
反向加载完整 Final Evaluation lane 集，并回答：

1. 完整矩阵必须包含哪些 lane；
2. 哪些 lane 可在当前 exact installed baseline 上执行；
3. 哪些 lane 必须交给对应目标平台重新解析、安装和验证等价 baseline；
4. 当前每个 lane 是 `recorded`、`missing` 还是 `stale`；
5. 尚有多少目标主机调度缺口。

本模块不执行远程任务、不接收自由路径或命令，也不把 coverage contract 冒充成完整行为评测。

## 为什么不能直接实现 6c2b

`ReleaseSlotStore` 对 installed slot 执行 host-target 强校验。macOS 上的 `darwin-arm64` slot 不能作为
Windows/Linux 行为证据；即使版本号相同，也必须由目标主机证明其 target-specific manifest、binary、boot、
launcher identity 与 runtime eval receipt。直接让本机聚合器创建三平台 lane 会绕过 ARC-07.5a-5g 的安装
与运行时身份边界。

因此 6c2a1 先冻结需求并显式区分：

- `local_installed_baseline`：lane platform 与当前 baseline target 的 OS 一致；
- `remote_target_required`：必须由目标平台产生独立 installed-baseline authority，后续才能签发执行授权与结果。

`darwin-*` target 规范映射为 Final Evaluation 的 `macos` platform；`linux-*` 与 `windows-*` 保持同名。
未知 target 或 `unknown` lane 失败关闭。

## 权威输入

Service 不接受调用方自报 lane、platform、suite 或 baseline。唯一输入是 `rollback request_id`，其余全部从
durable authority 反向解析：

```text
Rollback Request
  -> Proposal-bound rolled_back Outcome
  -> HAR-09.6c1 Runtime Verification
  -> exact active installed baseline identity
  -> HAR-09.6b Before/After Evidence
  -> proposal-bound Final Evaluation ID/SHA
  -> ordered interventional + adversarial lane set
  -> HAR-09.6c2a1 Coverage Contract
```

记录与读取时均重验 Outcome、Runtime Verification、Before/After 和 active baseline authority。任何 digest、
Proposal lineage、active slot 或 Final Evaluation lane set 漂移都会撤销 coverage view。

## Contract

`EvolutionPostRollbackBehavioralCoverageContract` 冻结：

- Outcome、Request、Workbench Session/Proposal；
- HAR-09.6c1 Verification 与 HAR-09.6b Evidence ID/SHA；
- Final Evaluation ID/SHA；
- baseline slot、manifest、version、target、source commit/tree；
- baseline platform；
- 连续排序的完整 lane 集；
- 每个 lane 的 kind、platform、suite、原 H5c、baseline cohort identity；
- 本机/远端 lane 数与原 adversarial required-platform 顺序。

固定权限位：

- `coverage_contract_recorded=true`；
- `behavioral_evaluation_recorded=false`；
- `execution_authority=false`；
- `learning_authority=false`；
- `promotion_authority=false`。

Contract 是目标集合，不是调度许可、远程结果、矩阵判决或自进化学习输入。

## 动态 Coverage View

读取 Contract 时逐 lane 查询 HAR-09.6c2a Store，并调用原 Service 动态复核完整 Release/Harness/Evolution
authority：

| 状态 | 含义 |
| --- | --- |
| `missing` | 未找到该 Outcome + 原 H5c 的 lane artifact |
| `recorded` | lane lineage 精确匹配且当前 authority 有效 |
| `stale` | 找到 artifact，但 lineage、Release receipt、H5c、active baseline 或 durable source 已失效 |

只有所有 Contract 依赖有效、baseline 仍 active、lane 数完整且每个 lane authority 有效时，view 才可机械显示
`matrix_ready=true`。即使如此，本模块仍固定 `behavioral_evaluation_authority=false`；真正的 6c2b 聚合器必须
独立签发完整矩阵 artifact 与 policy verdict。

`dispatch_required=true` 只投影到“远端且缺失”的 lane。已有但失效的远端结果不能被自动覆盖，必须先由后续
撤销/重试协议显式处理。

## 持久化与并发

- Evolution SQLite 使用参数化 SQL、`BEGIN IMMEDIATE`、Outcome/Request 唯一键；
- Contract JSON 上限 512 KiB；
- 写入时验证 HAR-09.6b durable evidence ID/SHA；
- 同一 Service 实例按 Request 加锁；多实例依靠唯一键和 content-addressed identity 收敛；
- 重复调用返回同一 Contract，不改变 lane 状态；动态状态只存在 View 中；
- 数据库篡改、未知字段、非法 target、重复/乱序 lane 或权限提权均失败关闭。

## 双通道与 UI

- Agent Tool：`evolution_post_rollback_behavioral_coverage(request_id)`；
- 共享 Slash：`/evolution outcome-behavior-coverage <rollback-request-id>`；
- CLI、Textual TUI 与 New UI 复用共享 Slash Router；
- normal 与 bypass 均无二次确认；lockdown 继续由统一 PermissionChecker 控制；
- 回执显示本机 baseline、已记录/缺失/失效计数、目标主机调度数与逐 lane 位置。

回执必须保留“本契约不授予远程执行、行为总体评测、学习或推广权限”的边界文案。

## 验收标准

- `darwin-arm64` 精确规范化为 `macos`；
- interventional macOS lane 标记为本机可执行，Windows adversarial lane 标记为目标主机所需；
- required-platform 顺序、lane 顺序与 Before/After Evidence 一致；
- 重复 Service 调用和 durable reload 返回同一 content-addressed Contract；
- 未记录 lane 投影为 missing，且只对远端 missing lane计入 dispatch；
- Before/After authority 失效后 matrix fail-closed；
- `unknown` platform 拒绝建立 Contract；
- Tool 与共享 Slash 返回同一 Contract；
- moderate/bypass 均不要求确认；
- 相关单元测试、Ruff、py_compile、文档治理和 diff check 通过。

## 后续切片

1. `HAR-09.6c2a2a Remote Lane Placement` 已绑定 exact active Worker incarnation，并从其 OS/CPU 合同导出
   exact release target；该 artifact 不验证健康、不预留容量、不授予执行权；
2. `HAR-09.6c2a2b / ARC-07.5h Target Baseline Resolution`：从受信任 Release Channel Catalog 解析与本机
   baseline source/version 等价的目标平台 artifact，不能只按版本字符串匹配；
3. `HAR-09.6c2a3 Remote Lane Dispatch + Claim`：绑定容量、健康、身份和 lease；
4. `HAR-09.6c2a3e Remote Execution Authorization` 已复用远程 revalidation 的 permission/run-grant/Ed25519
   模式并使用独立 post-rollback domain；Signed Result Ingestion 由 6c2a3f 继续；
5. `HAR-09.6c2b Post-Rollback Behavioral Matrix`：集合完整性、跨 lane verdict 与
   `behavioral_evaluation_recorded=true`；
6. Workbench/New UI/TUI typed coverage 详情投影；
7. `HAR-09.6d Long-Term Outcome Window`。
