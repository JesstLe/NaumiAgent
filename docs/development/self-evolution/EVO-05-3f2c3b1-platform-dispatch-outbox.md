# EVO-05.3f2c3b1 Required-Platform Dispatch Outbox

## 目标

把 Fresh Adversarial Matrix 中通过实时心跳和 admission kernel 的 `runnable` platform lane 转换为 durable queued dispatch。
该切片建立跨机器 transport 之前不可缺少的任务/Worker/capacity authority，但不把“已排队”冒充“远端已领取或已执行”。

## 权威绑定

每个 Dispatch 不可变绑定 Runtime Contract、Validation Plan、Source Snapshot、platform、suite、sample count、seed、probe
registry，以及 exact Worker id/instance/epoch/contract digest。`dispatch_id`、`job_id` 和 `reservation_id` 来自同一 canonical
payload digest，避免调用方替换其中任一 identity。

Service 每次重新执行 Matrix admission，并只接受 `runnable` lane；随后再次动态复验 Runtime Contract，调用现有 Worker
Registry 在 exact active incarnation 上预留一个物理 slot。Dispatch Store 在独立 `BEGIN IMMEDIATE` 中复核 Contract digest，
并保证同一 Contract/Platform 只有一个 queued artifact。Store 写入失败时立即补偿释放 reservation；进程在两步之间崩溃时，
已有 reservation TTL 负责机械回收。

## 状态边界

本切片只允许 `state=queued`，并固定声明：

- `capacity_reserved=true`；
- `worker_claimed=false`、`transport_delivered=false`；
- `execution_started=false`、`result_received=false`；
- `cohort_authority=false`、`comparison_authority=false`、`promotion_authority=false`。

因此本地 macOS 不能用这个 artifact 伪造 Windows/Linux 执行完成。下一切片必须增加远端认证 claim、租约续期、结果 manifest
回传和 H5a receipt 摄取，才可推动 lane 从 queued 走向 completed。

## 验收结果

- 8 路并发 queue 只形成一个 Dispatch 和一个 active capacity reservation；
- Dispatch 精确绑定 Worker incarnation、Contract、Source 与 evaluation 参数；
- pending/completed lane 不会排队或占用容量；
- 旧 Worker incarnation、容量耗尽、Contract stale、reservation 过期均失败关闭；
- Engine composition 与 lazy public export 已接入；
- 聚焦 Ruff、5 个 Dispatch/Matrix 测试与 import/YAML 通过；未运行全量测试。

## 后续

EVO-05.3f2c3b2 实现远端 Worker authenticated claim/result return，并把返回的 RED/GREEN sample artifacts 通过本地验证器摄取为
H5a authority。之后 matrix 才能基于真实跨平台 cohort 进入 completed；staged rollout 不得绕过该链路。
