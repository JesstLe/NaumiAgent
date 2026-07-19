# HAR-08.4e Governed Sandbox Eval Check Group Kernel

## 目标

把“一个 Eval sample 内按序执行一组 Profile checks”的最终执行治理从 Evolution 私有代码下沉到 Harness，
让普通 Sandbox Batch、EVO RED/GREEN 与后续 adversarial suite 共享同一条 ARC-04 authority/admission/lifecycle
路径。该切片不伪装成完整 Suite/Batch coordinator，也不新增 subprocess 旁路。

## Authority 与输入

`HarnessSandboxEvalExecutionKernel` 只接受：

- SHA-256 authority key、有限 sample index 与 `red|green|adversarial` lane；
- 1..80 个 typed、ID 不重复且保持调用方声明顺序的 `HarnessCheckSpec`；
- 当前 Profile digest 与异步 trust/current revalidator；
- 精确 Git revision/tree，以及可选的 HAR-08.4d source overlays/current callback；
- 可执行父权限回执和绑定 parent/run/grant ID/digest 的 `HarnessSandboxEvalRunAuthority`。

执行前重新读取父回执，要求其可执行并显式委托 `bash_run`；随后通过共享
`RunDelegationGrantAuthority` 复验 grant 当前仍 allowed，且 parent、run、grant identity 与 delegated scope
完全一致。构造时还要求 Runner、Run Grant、Permission Store 与 Composer 属于同一 workspace/authority
composition；调用方不能只传一个 grant ID 或跨工作区依赖绕过 durable authority。

## 成组执行与清理

Kernel 为每个 check 派生稳定、lane 隔离的 run ID，逐项调用 `HarnessSandboxCheckRunner`。每一项的 admission
都必须经 `ShellWorkerAdmissionComposer` 消费同一 Run Grant，并且：

1. 单项 check 最多 admission 一次；
2. 无论 Runner 成功、失败或抛错，已创建的 composed authority 都在 `finally` 中 release；
3. Profile/source current callback 继续由 Runner 在 snapshot、admission 和终态边界复验；
4. 整组只有在每项都产生 ARC-04 job ID 与 lifecycle receipt digest 时才返回；
5. 返回保持原始 typed `HarnessSandboxCheckResult`，由上层唯一的 Suite builder 组装 H5a evidence。

adversarial run ID 额外绑定 lane authority key，避免不同平台或 RED/GREEN adversarial lane 在共享
parent/sample/check 时发生碰撞；既有 Evolution RED/GREEN run ID 保持不变。

Kernel 不取得或撤销 cohort/Runtime lease，不持久化 H5a，也不决定 RED/GREEN/adversarial 的指标、Identity、
Policy 或 receipt。authority 生命周期和证据语义仍由各自 coordinator 负责。

## 迁移与验收证据

- `EvolutionInterventionalSampleKernel` 已删除私有逐 Check admission/cleanup 循环；
- standalone RED sample 通过共享 kernel 真实执行精确 Git revision，生成 lifecycle evidence，并正确撤销自己
  的 Runtime lease/Run Grant；
- 完整 RED→GREEN cohort 继续通过 cohort-scoped grant、中断恢复、candidate overlays、H5a/H5c 与 Failure
  Attribution 链路；
- 非 SHA authority、越界 sample 与重复 check 在读取权限/执行命令前 fail closed；
- Ruff、编译和定向小模块测试通过，未运行全量测试。

## 当前边界与下一步

HAR-08.4f 已在 Harness 中用本 kernel 编排连续 sample、Run Grant 生命周期、H5a 前缀恢复与 partial
checkpoint。EVO-03.6c 已用本 kernel 真实执行 adversarial RED revision 和 GREEN overlay，并写入 H5a。
HAR-08.4 仍为 partial：通用 Service/Tool/UI surface、跨 Batch admission/backpressure 和 Linux/Windows CI
尚未完成；EVO-03.6 后续只接入现有 Batch coordinator 与 H5c，不能复制执行循环。
