# EVO-05.3c Harness Revalidation Evidence

## 1. 目标

把 EVO-05.3b1 Exact Replay 或 EVO-05.3b2b successful Rebase 从“源码可重放”推进为“项目代码已经由当前受信任
Harness Profile 真实验证”。本切片签发新的、与 execution/result/profile/check plan 精确绑定的证据，但仍不复用旧
Evaluation/Approval，不授予 Promotion。

## 2. 精确源码重新物化

Replay/Rebase 的 disposable worktree 已在前序切片清理，因此验证不能依赖残留目录。Executor 重读 active Candidate Lease：

1. 按 Promotion Patch Manifest 复核 Candidate 与 baseline 的真实字节、diff 和 executable mode；
2. Exact 路径直接构造 Candidate overlays；
3. Rebase 路径从 frozen current target revision 重读 Git blob，再次执行相同三方合并；
4. 每个结果 digest 和 merge strategy 必须与 durable Replay/Rebase artifact 一致；
5. 以 `git ls-tree` SHA-256 绑定 target revision，以 canonical overlay manifest SHA-256 绑定候选结果。

任何 Lease 漂移/过期、target 变化、结果不可重复或 digest 不一致都会 fail closed。

## 3. Harness 与 ARC-04 真实执行

`HarnessService.prepare_evolution_revalidation()` 从当前受信任 `.naumi/harness.yaml` 选择所有满足：

- `required_for: change`；
- `when_changed` 匹配批准改动路径；
- typed `HarnessCheckSpec` 与当前 profile digest。

执行时 `HarnessSandboxCheckRunner` 从 frozen Git revision 创建一次性 snapshot，再应用受摘要约束的 overlays。每项检查通过
`ShellWorkerAdmissionComposer` 进入 ARC-04 Worker，网络关闭、依赖安装不自动开放，用户工作区和 Candidate worktree 均不写入。
Profile 和 source authority 在快照前、admission 前及执行后重新验证。

Agent Tool：

```text
evolution_revalidation_validate(request_id=...)
```

手动入口：

```text
/evolution revalidation-validate <revalidation-request-id>
```

两者都持久化精确父权限回执并委托 `bash_run`。bypass 直接签发 bypass authority；其他允许模式签发 policy authority，
不会弹出高风险二次确认。Executor 本身仍不能绕过 Request/Input/Lease/Profile/digest authority。

## 4. Durable Receipt 与并发恢复

`EvolutionRevalidationValidationReceipt` 使用 `evolution-revalidation-validation-v1`，绑定：

- Request 与 successful Replay/Rebase identity；
- target revision/tree 与 overlay source digest；
- Harness profile/check plan digest；
- epoch、每项 check status、snapshot manifest、job id、lifecycle receipt、output digest、exit code 和耗时。

SQLite 以完整 authority digest 为幂等键；active claim 阻断跨进程重复执行，过期 claim 增长 epoch，旧 executor 无法提交。
如果后续检查异常，已经执行的 check/job/lifecycle 证据仍保留，但整体状态为 failed，不能误报“零执行”或“全部通过”。

Receipt 明确区分：

- `project_code_executed`：至少一个检查形成完整 Worker evidence；
- `all_checks_passed`：计划完整且全部通过；
- `new_validation_evidence_issued`：产生了本轮新证据；
- `old_evidence_invalidated=false`、`promotion_authority=false`：留给 EVO-05.3d 收口。

## 5. 验收证据

- Exact Replay 可重新构造 Candidate bytes，并在不修改 main/source 的情况下形成 passed Receipt；
- Rebase success 可从 current target + Candidate 再现相同 result digest；
- failed check 仍保存真实 job/lifecycle evidence，但不产生 Promotion authority；
- 后续基础设施异常时保留已完成的部分 Worker evidence，整体 fail closed；
- Harness adapter 拒绝不匹配的 Tool、arguments 或缺少 `bash_run` delegation；
- Tool 与 Slash 共用同一 Service，权限规则为 medium、无二次确认；
- 真实 Sandbox Runner overlay 单测执行了本机 Worker、验证 snapshot 内候选内容并确认原工作区未变化；
- 仅运行 Revalidation 和单个真实 Sandbox overlay 测试，未运行全量测试。

## 6. 当前边界

- 本切片运行 Profile checks，但不会把旧 Final Evaluation、Approval Decision 或 Promotion Package 自动改写为 current。
- EVO-05.3d 必须签发新的 Revalidation Outcome，显式标记旧 evidence stale，并机械决定是否需要重新聚合专业审批。
- Candidate 长期恢复仍依赖未来不可变、加密、受保留策略治理的 blob authority；active Lease 不是永久归档。
