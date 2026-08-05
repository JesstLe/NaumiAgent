# EVO-05.4b2 Real Local-Canary Executor

## 目标

把 EVO-05.4b1 的短期 stage-entry authority 变成一次可验证的真实 local-canary 运行，但不扩大到 opt-in、percentage、stable、
Git、merge、push 或发布。本切片只消费 immutable GREEN source，并通过 HAR/ARC-04 的既有执行内核运行 exact Harness Profile。

## 权威链

执行器每次运行都重新读取以下事实，而不是相信调用参数或历史 UI 状态：

1. HMAC-attested local-canary entry 当前仍可执行，且 workspace kill switch 未暂停；
2. entry 绑定的 immutable Rollout Plan 与 Fresh Runtime Contract 仍一致、可执行；
3. content-addressed Evaluation Source 能物化 exact GREEN，snapshot/tree digest 与 Contract 完全相同；
4. 当前 Harness Profile 仍受信任，且 check ID、完整 `HarnessCheckSpec` digest、总 timeout 与 Validation Plan 完全一致；
5. 父 Permission Receipt 仍授权执行，并显式委派 `bash_run`；
6. runtime Run Lease 与短期 Run Delegation Grant 均由现有 HAR authority 签发。

任一条件失效都会 fail closed。`bypass` 仍可免交互确认，但不会绕过 immutable source、Profile、lease、grant、kill switch 或 journal。

## 真实执行

`EvolutionRevalidationLocalCanaryExecutor` 调用 `HarnessSandboxEvalExecutionKernel` 的 `sandbox` lane。实际命令由
`HarnessSandboxCheckRunner` 物化到一次性 snapshot，经 `ShellWorkerAdmissionComposer` 派生 child permission、Tool Job、
Execution Grant，再由 ARC-04 authenticated local Shell Worker 在 network-deny sandbox 中执行。

每个 check 的终态证据至少包含：

- check/run/job identity；
- ARC-04 lifecycle receipt digest；
- immutable source tree digest；
- sandbox snapshot manifest digest；
- exact Profile digest；
- exit code 与 duration。

缺少任一 ARC-04 执行字段时，本轮不能标记为 passed。

## Crash-safe journal

SQLite journal 使用 append-only content-addressed event chain：

`admitted → source_verified → running → passed|failed|cancelled`

- admission 在 `BEGIN IMMEDIATE` 中分配 deterministic run ID/index；
- 同一 entry 已有未完成 run 时，重启会恢复该 run，而不是消耗新的样本预算；
- `running → running` 只用于进程丢失后的新 lease/grant 重试，旧 grant 必须先过期或被 fencing；
- terminal event 继承 exact source/grant evidence，并记录全部 check evidence；
- terminal 写入失败时保留 running journal；下一次调用重新执行并补齐 terminal，而不伪造完成；
- task cancellation 先撤销 grant、释放 lease、写入 `cancelled`，再向上层传播取消；
- worker 完成后再次读取 kill switch；期间发生 pause 时，本轮记录 `cancelled`，不能算作通过。

Journal 的 terminal 状态只开放 `monitor_authority`。所有后续 stage 与 promotion authority 始终为 false。

## 运行预算

Rollout Plan 的每阶段样本数被限制为最多 100，local-canary run index 固定为 `0..99`。风险档位的 base run 数为：

- low：10；
- medium：15；
- high：20；
- critical：25。

percentage/stable 阶段最多扩展为 base 的 3/4 倍，critical stable 上限恰为 100。该边界避免未受控的无限自动执行；是否达到
观察窗口与样本阈值由 EVO-05.5 monitor 决定。

## 验收结果

- immutable GREEN、current Contract、current Profile 与 entry digest 全链复验；
- 真实 ARC-04 sandbox Worker 执行 5 个 exact Profile checks，job/lifecycle/source/snapshot 证据完整；
- 父权限无 `bash_run`、Profile 漂移、source mismatch 或 check 失败均无法通过；
- terminal journal 写入前模拟进程丢失，下一次调用恢复同一 run 并形成合法 hash chain；
- worker 运行期间触发 kill switch，结果机械收口为 cancelled；
- asyncio task cancellation 不被吞掉，且 grant 已撤销、runtime lease 已释放、cancelled 已持久化；
- Engine 已装配 production service，公共 lazy export 可用；
- 只运行相关模块测试，未运行全量测试。

## 当前不足与下一切片

本切片证明“候选真实运行并留下可信终态”，还没有证明“候选长期更好”。
[EVO-05.5a](EVO-05-5a-rollout-monitor-baseline.md) 已从 Fresh Final/Interventional GREEN raw H5a 冻结可信比较基线。
EVO-05.5b 必须从 terminal journal 构建 runtime monitor：

1. 聚合 completed run 数、错误率、p95 latency、completion rate 与 cost；
2. 绑定 Rollout Plan 的 frozen threshold 和 minimum observation window；
3. 纳入用户撤回、security incident、data-integrity 与 kill-switch 信号；
4. 对 insufficient/passing/breached 形成 content-addressed observation receipt；
5. breach 只能自动 pause/请求 rollback，passing 也不能直接开放下一 stage。

随后仍需 EVO-05.6 automatic rollback 与 EVO-05.7 promoted/rolled-back Outcome 回注。三者完成并做真实 patch 演练前，不得宣称
自进化真实闭环完成。
