# HAR-09.6c1 Post-Rollback Runtime Verification

## 状态

已实现。

## 目标

在 Proposal-bound `rolled_back` Outcome 已形成后，对当前 active baseline installed slot 执行一轮新的、真实的
runtime 恢复验证：重新运行后端 binary 的受控 `--version` boot probe，并重新解析、持久化 launcher identity。

本切片回答“回滚后的 baseline bytes 现在是否仍可启动，launcher 是否仍解析到 exact baseline identity”。它不复用
Rollback Execution 内的旧 Boot Receipt 冒充新证据，也不声称业务行为、模型调用、工具语义或长期指标已经恢复。

## 最小依赖链

```text
Workbench Proposal
  -> Rollback Outcome
  -> Rollback Execution Receipt
  -> rollback Active Pointer
  -> exact baseline installed slot
  -> fresh ReleaseSlotBootReceipt (--version)
  -> fresh ReleaseLaunchResolution (status-only, no process start)
  -> Post-Rollback Runtime Verification
```

record 前后都动态复验 Outcome 与 Execution authority；只有 baseline 仍是 active runtime 时才允许启动 fresh probes。

## Fresh probes

### 1. Fresh boot

`ReleaseSlotStore.verify_bootable()` 会再次：

- 校验 manifest、bundle 文件摘要与不可变属性；
- 选择 host-compatible baseline backend；
- 在 20 秒硬上限内执行 exact `binary --version`；
- 限制输出为 64 KiB、要求 UTF-8、exit 0 且版本与 manifest 精确匹配；
- 持久化新的 content-addressed `ReleaseSlotBootReceipt`。

### 2. Fresh launch identity

`resolve_and_record_launch(process_start_requested=false)` 会重新读取 current pointer、slot、pointer-bound Boot Receipt 与
backend bytes，形成新的 `ReleaseLaunchResolution`。它不启动用户进程、不保存参数、不修改 workspace 或 Git。

Verification 强制 fresh boot 与 fresh launch 具有相同 slot、slot SHA、binary SHA、version 和 target；launch pointer
必须等于 Rollback Execution 的 pointer ID/SHA/generation。

## Artifact 与权限

`EvolutionPostRollbackRuntimeVerification` 冻结：

- Outcome、Rollback Execution、Workbench Proposal、Experiment Contract 与 Candidate lineage；
- baseline slot/manifest/version/target；
- rollback pointer identity；
-完整 fresh Boot Receipt 与 fresh Launch Resolution；
- runtime identity SHA-256；
-固定五项 checks：active pointer、immutable bundle、backend binary、version output、launch resolution。

它固定：

- `post_rollback_verification_recorded=true`；
- `post_rollback_evaluation_recorded=true`，口径仅为 installed-runtime mechanical verification；
- `behavioral_evaluation_recorded=false`；
- `long_term_metrics_recorded=false`；
- `learning_authority=false`、`promotion_authority=false`。

因此 UI 必须同时显示“恢复评测已记录”和“行为级 Eval 尚未记录”，不能省略后者。

## 持久化、并发与双库边界

Evolution/session DB 与 Release Slot DB 是不同 SQLite authority store，不能声称跨库原子提交。

- Evolution Store 使用参数化 SQL、`BEGIN IMMEDIATE`、Outcome/Execution ID+SHA 复验和 512 KiB artifact 上限；
- Outcome、Request、Workbench Proposal 均唯一；
- 单进程以 request lock 避免重复探针；
- 多实例可同时完成合法 fresh probes，Evolution Store 采用 first-valid-writer-wins，同一 lineage 的竞争者读取同一
  immutable verification；不同 lineage 冲突失败；
- Service 在 probes 后和 evidence 写入后再次完整复验；中途漂移返回
  `post_rollback_authority_changed`；历史 artifact 可保留，但 Workbench projection fail-closed。

读取时重新验证 Outcome/Execution、fresh Boot Receipt、fresh Launch Resolution 及完整 artifact digest。后续版本槽推进
不会抹除历史 verification，但 `active_baseline_authority` 会变为 false。

## 双通道与 UI

- Agent Tool：`evolution_post_rollback_runtime_verification(request_id)`；
- 共享 Slash：`/evolution outcome-verify-runtime <rollback-request-id>`；
- Workbench/New UI/TUI 显示 Verification ID、baseline slot/version 和 fresh boot + launch identity；
- strict frontend protocol 拒绝 process-start 提权、跨 Outcome/Proposal/Candidate 绑定、错误 pointer/slot/binary linkage，
  以及 behavioral/long-term/learning/promotion 越权。

该动作会运行已安装 baseline binary 的固定 `--version` 探针，但不运行自由命令、不接受路径、不联网、不修改代码、Git
或发布状态。normal 与 bypass 都不需要二次确认；lockdown 仍由统一 PermissionChecker 控制。

## 验收证据

- 真实临时 Git baseline/candidate 和两个 POSIX closed bundle 完成 install、activate、rollback；
- Outcome 后生成新的 Boot Receipt 与新的 Launch Resolution，二者均不同于 rollback 时旧 receipt；
- 8 个独立 Service 并发执行收敛到同一 Verification artifact；
- invalid Request ID 在访问存储和运行 binary 前拒绝；
- Launch Resolution row 篡改后动态撤销 verification authority；
- Agent Tool 与共享 Slash 显示同一 Verification ID；
- Workbench、New UI、TUI 只读显示相同口径；
- 仅运行相关 Python/Node 小模块测试、Ruff、py_compile 和 diff check。

## 未完成边界

HAR-09.6 仍为 partial：

1. `HAR-09.6c2a` 已把 ARC-07.5f/5g transport 绑定到 6c1、原 H5c lane、原 baseline cohort 与 fresh
   installed-runtime H5a/H5c；单个平台 lane 仍固定 `behavioral_evaluation_recorded=false`。下一步 6c2b
   聚合原 Final Evaluation 的完整平台/lane 集合；
2. `HAR-09.6d Long-Term Outcome Window`：窗口、样本覆盖、censoring 和持续健康指标；
3. promoted Outcome、supersede ledger；
4. ARC-07.6 配置/数据 snapshot 恢复路径；
5. Windows 真实 `.exe` fresh-probe 验收。

6c1 不能被上述模块当作替代证据，也不能进入 EVO-06 policy learning。
