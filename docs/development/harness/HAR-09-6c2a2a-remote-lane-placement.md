# HAR-09.6c2a2a Remote Lane Placement

## 状态

已实现。

## 目标

为 HAR-09.6c2a1 Coverage Contract 中一个 `missing` 且 `remote_target_required` 的 lane 选择 exact active
Worker incarnation，并从 Worker OS/CPU 合同机械导出唯一 release target。

本切片只回答“后续应由哪一个 Worker incarnation、针对哪个 target 继续准备”。它不是调度、健康检查、容量预留、
baseline 下载/安装、远程执行或结果摄入。

## 依赖链

```text
HAR-09.6c2a1 Coverage Contract
  -> one missing remote lane
  -> WorkerRegistry active incarnation
  -> tamper-evident Worker Contract
  -> exact platform + architecture normalization
  -> Remote Lane Placement
```

Service 只接受 `rollback request_id`、原 Final Evaluation `comparison_id` 和 `worker_id`。Lane、platform、suite、
Worker instance/epoch、capabilities、isolation 和 target 均从 durable authority 读取，调用方不能自报。

## Worker Contract Gate

Placement 要求 Worker Contract：

- kind 为 `tool`；
- protocol range 包含 v1；
- system 与 Coverage lane platform 精确匹配，`darwin` 规范映射为 `macos`；
- 声明 `shell_non_pty`、process-tree cancel、ephemeral workspace、network policy、environment allowlist、
  resource limits 和 artifact digest；
- isolation contract 对上述六项全部为 true；
- Contract digest 有效，且 WorkerRegistry 当前 active pointer 仍指向同一 instance/epoch/SHA。

本模块没有 durable Worker Health Report authority，因此不能把 capability 声明扩大为“当前健康”。实时 heartbeat、
accepting-jobs、active jobs、资源余量和 capacity reservation 必须由后续 Dispatch Gate 重新验证。

## Exact Release Target

Target 只允许以下规范化映射：

| Worker system | machine aliases | release target |
| --- | --- | --- |
| `darwin` | `arm64` / `aarch64` | `macos-arm64` |
| `darwin` | `x86_64` / `amd64` / `x64` | `macos-x64` |
| `linux` | 同上 | `linux-arm64` / `linux-x64` |
| `windows` | 同上 | `windows-arm64` / `windows-x64` |

未知 system/machine 失败关闭。Placement 不能仅按 `windows` 等 platform 名猜测 archive target。

## Artifact 与权限边界

`EvolutionPostRollbackRemoteLanePlacement` 冻结：

- Coverage、Outcome、Request、Proposal identity；
- 完整 expected lane；
- Worker ID、instance、epoch、Contract SHA、software version、system/machine；
- exact release target；
- required capability/isolation contract；
- content-addressed Placement ID/SHA 与 placed timestamp。

固定状态：

- `worker_registration_verified=true`；
- `target_compatibility_verified=true`；
- `health_verified=false`；
- `capacity_reserved=false`；
- `baseline_resolved=false`；
- `transport_delivered=false`；
- execution/result/learning/promotion authority 全部为 false。

## 动态撤权

每次读取重新验证：

1. Placement durable JSON 未损坏；
2. Coverage Contract ID/SHA、Outcome、Proposal 和 lane 仍精确一致；
3. Coverage authority 有效且 lane 仍为 missing remote lane；
4. WorkerRegistry active pointer 仍指向 exact instance/epoch/Contract；
5. Worker Contract digest、platform、target、capability 和 isolation 仍满足要求。

Worker revoke、更高 epoch takeover、Coverage lane 已被填充、active baseline 漂移或任一数据库篡改都会令
`placement_authority=false`。同一 Coverage lane 只能绑定一个 Placement；更换 Worker 需要后续显式撤销/重置协议，
不能静默覆盖。

## 双通道

- Agent Tool：`evolution_post_rollback_remote_lane_placement(request_id, comparison_id, worker_id)`；
- 共享 Slash：`/evolution outcome-place-behavior <rollback-request-id> <comparison-id> <worker-id>`；
- CLI、Textual TUI 与 New UI 复用同一 Slash Router；
- normal 与 bypass 均无二次确认，因为本动作不下发、不占用容量、不执行；lockdown 仍由 PermissionChecker 控制。

回执明确显示“Health / capacity 尚未验证、尚未预留”和无执行权边界。

## 验收证据

- 真实 WorkerRegistry 注册 Ed25519-independent tamper-evident Worker Contract；
- Windows `AMD64` 精确产生 `windows-x64`；
- 重复 Service、Tool、Slash 收敛到同一 Placement；
- moderate/bypass 均无确认；
- local lane 拒绝 placement；
- Linux Worker 不能承接 Windows lane；
- Worker revoke 后动态撤销 placement authority；
- 相关小模块测试、Ruff、py_compile、import、文档治理和 diff check 通过。

## 后续切片

1. [HAR-09.6c2a2b / ARC-07.5h Target Baseline Resolution](HAR-09-6c2a2b-target-baseline-resolution.md) 已用
   Placement 的 exact target 从受信任 Catalog 解析与本机 baseline 同 version/source commit/source tree 的
   target-specific Build Attestation，并保持未下载、未安装、无执行权；
2. `HAR-09.6c2a3 Remote Dispatch/Claim`：验证 fresh health、容量与 lease；
3. `HAR-09.6c2a3e Execution Authorization` 与 `6c2a3f Signed Result Ingestion` 已完成；
4. `HAR-09.6c2b1 Post-Rollback Behavioral Matrix Core` 与 6c2b2 typed 双端详情均已完成；下一步为 6d 长期 Outcome window。
