# EVO-03.6c Adversarial Lane Sample Executor

## 目标

把 EVO-03.6b 的一个精确平台/阶段 lane 和一个 sample 交给 HAR-08.4e 执行，并生成可恢复的 H5a 结果与
防篡改回执。该切片只实现 sample adapter；连续 5..100 samples 的 Runtime lease、Run Grant、恢复和清理仍由
HAR-08.4f 负责，Evolution 不复制 Batch coordinator。

## Authority 与执行边界

`EvolutionAdversarialSampleExecutor` 每次执行都会重新解析并交叉校验 Batch Request、Probe Contract、Validation
Plan 和 Experiment Lease。执行前还必须满足：

- durable Lease 与调用方 Lease 完全相同、仍为 active、worktree ready 且未过期；
- 当前真实平台与选中 lane 完全一致，平台不匹配时在读取 Profile/父权限前阻断；
- 当前 Harness Profile 仍可信，Profile SHA、check spec、argv、timeout 和 adversarial probe tags 未漂移；
- `lane_order`、`sample_index` 拒绝 bool、越界值与被篡改模型；
- 外部传入的 `HarnessSandboxEvalRunAuthority` 由 Batch coordinator 管理，sample executor 不签发或撤销它。

每条 lane 通过 `adversarial_lane_authority_key()` 获得稳定 authority。HAR-08.4e 的 adversarial check run ID
同时绑定该 authority，避免同一 parent/sample/check 在不同平台或 RED/GREEN lane 之间碰撞；既有 RED/GREEN run
identity 保持兼容。

## RED、GREEN 与证据

- RED 从精确 baseline commit/tree 物化源码；
- GREEN 捕获受管 candidate worktree Snapshot，以 typed overlays 覆盖同一 baseline；
- 两者在 H5a 写入前和返回后都重新验证 Lease/source，GREEN 同时重验 candidate Snapshot；
- 每个 Profile check 由 ARC-04 Worker 实际执行，case 保存 lifecycle receipt SHA 和 Run Grant SHA；
- 非 runner error 的 case 写入 `adversarial.<check_id>.exit_zero` 数值 observation，成功为 `1`、失败为 `0`、
  target 为 `1`；runner/evaluation error 不伪造数值结果；
- Suite 使用现有 `HarnessEvalSuiteResult` 和 `HarnessStore` H5a，不创建 Evolution 私有结果仓库。

已存在的 H5a sample 会在不读取已撤销父权限的情况下幂等返回，但必须重新验证 Request/Profile/Lease/平台、
source identity、case 顺序、runner、lifecycle 和 grant evidence；同键不同结果以 conflict 阻断。

## 验收证据

- 真实临时 Git workspace 完整建立 Mutation、Validation、trusted Profile、Probe Contract 与 Batch Request；
- macOS RED 真实执行 baseline check，GREEN 真实执行 candidate overlay check，二者写入独立 H5a lane；
- 每个 case 均产生 ARC-04 job/lifecycle、同一 Run Grant 和 `exit_zero=1` observation；
- 外层 Batch owner 撤销 grant/释放 Runtime lease 后，已持久化 sample 仍可幂等读取；
- 平台 receipt 篡改、错误平台、bool sample index 和 GREEN candidate 漂移均以稳定错误拒绝；
- 不同 adversarial lane 的 authority key 与 run ID 隔离，既有 RED run ID 不变；
- Ruff、编译和 13 项定向测试通过，包含真实 RED/GREEN 执行；未运行全量测试。

## 当前不足与下一步

本切片没有自己循环执行 5 个样本，也没有生成 RED/GREEN H5c comparison、跨平台调度或最终 Evaluation
Receipt。下一步 EVO-03.6d 只需把一个 lane 接入 HAR-08.4f：coordinator 签发一次短期 grant，按连续 H5a 前缀
调用本 executor，验证 sample receipt 后负责撤销与恢复。完成单平台 RED lane 和 GREEN lane 的连续闭环后，
再推进 H5c；Linux/Windows dispatcher 必须复用同一 contract，不能在本地伪造平台结果。
