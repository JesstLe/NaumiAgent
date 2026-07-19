# EVO-03.6d Adversarial Lane Continuous Cohort

## 目标

把 EVO-03.6c 的真实单 lane/sample executor 接入 HAR-08.4f，让一个当前平台的 RED 或 GREEN lane 在同一轮
Runtime lease/Run Grant 下连续执行 5..100 个 H5a sample，并支持从已持久化前缀恢复。Evolution 只提供 authority
adapter 和防篡改完成回执，不复制 Harness Batch coordinator、ARC-04 worker 或权限状态机。

## 执行边界

`EvolutionAdversarialCohortExecutor` 在读取父权限前先调用 sample preflight，重验 Batch Request、Probe Contract、
Validation Plan、Experiment Lease、当前平台、可信 Profile 和 GREEN candidate snapshot。composition 必须满足：

- cohort、sample executor 与 Harness Store 属于同一 workspace 和同一对象图；
- lane order 必须精确指向 Request 中的当前平台 RED/GREEN lane，bool 与越界输入在权限读取前拒绝；
- authority key 复用 `adversarial_lane_authority_key()`；进度仍由 HAR-08.4f 的 `adversarial` lane checkpoint 表达；
- 未完成前缀才读取父回执、取得 Runtime lease 并签发只允许 `bash_run` 的短期 Run Grant；
- 每个新 sample 由 EVO-03.6c 经 HAR-08.4e/ARC-04 执行并先写入 H5a，随后 coordinator 才确认进度；
- 完整前缀可在不读取旧父回执和已撤销 Run Grant 的情况下幂等重建 receipt，但仍重新验证 Lease/Profile/source。

## 完成回执

`EvolutionAdversarialCohortReceipt` 绑定 request/probe/plan/lease、lane/platform/phase、authority key、连续 sample
seed/result/receipt digest、跨恢复 epoch 的 Run Grant digest 集合、source/baseline identity，以及每个 check 的
passed/failed/evaluation-error 计数和逐样本 `exit_zero` 数值。Evaluation error 使用 `null` 保留精确位置，不能伪造
为失败值 0。Receipt digest 和 ID 覆盖全部字段，RED 禁止 overlay，GREEN 必须绑定 overlay。

## 验收证据

- 真实临时 Git workspace 先分别形成 RED/GREEN sample 0，再由共享 coordinator 从 1/5 恢复到 5/5；
- 两条 lane 的 20 次 Profile check 均经 ARC-04 worker 执行，H5a 连续、每个 check 的五个数值均为 1；
- 每条 lane 的 receipt 同时绑定初始 grant 与恢复 cohort grant，最终均已撤销且 Runtime lease 已释放；
- 完整 RED lane 使用不存在的 parent receipt 仍可幂等返回，证明没有重新读取执行权限；
- receipt 数量篡改被模型一致性校验拒绝；既有 HAR-08.4f 中断恢复与 6c 平台/漂移用例无回归；
- 仅运行 adversarial、Sandbox Batch、Ruff 与编译定向验证，不运行全量测试。

## 当前不足与下一步

本切片只完成当前真实平台上的单 lane 连续闭环，不调度 Linux/Windows，也不比较 RED/GREEN。下一最小切片应实现
EVO-03.6e：严格配对同平台 RED/GREEN cohort receipt，复用现有 H5b2/H5c comparator 形成 adversarial verdict；
不得另建 Evolution 评分器。跨平台 dispatcher、通用 Harness Service/Tool/UI 与最终 EVO-03.7 Evaluation Receipt
仍需独立切片。
