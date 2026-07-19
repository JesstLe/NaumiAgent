# HAR-08.4a 单项 Sandbox Profile Check Runner

## 1. 交付目标

在不改写现有 Eval Suite/Batch 的前提下，让一个已受信任的 `HarnessCheckSpec` 能通过 ARC-04.3a 的真实隔离
Worker 执行。这个切片只冻结可信快照、admission 接口和结果证据，不宣称 HAR-08.4 已全部完成。

## 2. 快照与信任链

- 执行前、admission 前和终态后分别复验 Profile trust/digest；任一阶段漂移即阻断或标记 stale；
- 对源工作树计算 fingerprint，并只复制 `git ls-files -co --exclude-standard` 的有序唯一文件到一次性
  snapshot；拒绝 symlink、路径逃逸、文件/总字节/manifest 上限；
- `.env*`、私钥/证书、credentials/service-account 与 `.naumi` 中除 `harness.yaml` 外的内容禁止进入快照；
- snapshot manifest 记录 run/check/source/profile/file digest，并绑定到 `ShellCommandSpec`；HAR-08.4c 已将
  manifest 升级为 v2，区分 working tree 与精确 Git revision；
- 检查只能写 snapshot，原工作树执行前后 fingerprint 必须一致；snapshot 始终清理，artifact 独立持久化。

## 3. Authority 边界

Runner 不自行判断 bypass、不伪造用户确认，也不直接签发 grant。调用方必须提供 `SandboxJobAdmitter`，返回已经
由 ARC-04.2a-2c 完成 permission receipt、Tool lease、Worker registration/health、ExecutionGrant 和 ToolJob
admission 的 `AdmittedSandboxShellJob`。Runner 只校验 Shell spec/Job 参数/identity 完全一致后调用 Coordinator。

HAR-08.4b 已把本 Runner 接入生产 Engine；本文件继续只定义 Runner 自身合同，生产授权和双通道接线见
`HAR-08-4b-production-sandbox-check-surface.md`。

## 4. 验收证据

- 真实 Git 仓库快照内运行受信 Profile check，结果为 passed、artifact 与 lifecycle receipt 可定位；
- 检查对 snapshot 的写入不会污染源工作树，结束后 snapshot 删除；
- 未受信 Profile 在目录创建和 admission 前阻断；
- 已跟踪敏感文件使快照 fail closed，且不会调用 admission；
- macOS 真实 sandbox 端到端通过，而非 mock subprocess。

## 5. 未完成项

- HAR-08.4b 已完成 Runner cleanup、任务局部外层授权 context 和 Slash/Agent Tool 共路由；
- 尚未接 Eval Suite/重复 Batch/Baseline Comparator，也没有并行调度、缓存或 UI typed progress；
- 尚未为自进化候选建立 baseline/candidate 两组干预 cohort；EVO-03 只能把本切片视为执行前置；
- Windows fail closed，Linux bwrap 仍需平台 CI 证据。
