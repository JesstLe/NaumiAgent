# HAR-08.4f Resumable Sandbox Batch Coordinator

## 目标

把连续 sample、单一 Runtime lease/Run Grant、H5a 前缀恢复、真实进度和异常清理收敛为 Harness 级 Batch
coordinator。普通 Sandbox Eval、EVO RED/GREEN 和后续 adversarial suite 均不得复制这套状态机。

## Coordinator authority

`HarnessSandboxBatchCoordinator` 绑定一个真实 workspace，并要求 `RunDelegationGrantAuthority` 与传入的
Permission Store 属于同一 workspace/composition。每次执行机械验证：

- lane 是 `red|green|adversarial`，authority key 是 SHA-256；
- requested samples 为 5..100，总时限为 60..3600 秒；
- H5a records 是从 0 开始且不超过请求数量的连续前缀；
- 上层重建的 typed sample receipts 与每个 H5a `sample_index` 一一对应；
- lane-specific lifecycle/grant/identity evidence 在恢复前、每个新 sample 后及 completion 前都重新验证。

完整前缀在读取父权限前幂等返回。未完成 Batch 才读取可执行父回执、取得独占 Runtime lease，并签发一个
只委托 `bash_run` 的短期 Run Grant。缺失 samples 共享该 grant 调用 HAR-08.4e check-group kernel；上层 sample
executor 必须在返回前完成 H5a immutable write。

## 恢复、进度与 partial checkpoint

每个 sample 返回后，Coordinator 不相信回调的“已完成”声明，而是重新读取 H5a，再验证 records/receipts/
run evidence，然后才发送 `HarnessSandboxBatchCheckpoint`。Checkpoint canonical digest 绑定：

- authority key、lane、stage 与 requested/persisted 数量；
- ordered H5a Result digests；
- 当前 run ID、Run Grant digest、稳定 code 与时区时间。

stage 覆盖 `recovering/acquiring/executing/completed/failed`。sample 抛错时先重读 H5a，随后在 `finally` 中
撤销 grant/释放 lease，清理完成后才发送 `failed + sample_execution_interrupted`；所以 observer 卡顿不会延迟
高权限清理，“命令已写入 H5a、receipt 返回前中断”也仍能显示真实持久前缀。Progress observer 有 1 秒交付
上限，异常只记录 warning，不中断执行。Checkpoint 当前通过 callback 交付，不新增数据库；其引用的 sample
facts 已由 H5a 持久化。

## Authority cleanup

成功、sample 异常和取消都在 `finally` 中尝试：

1. 撤销本轮 Run Grant；
2. 释放精确 owner/epoch 的 Runtime lease；
3. 两项清理互不跳过，任一失败以稳定错误暴露。

Harness 原生 owner/idempotency/revoke namespace 使用 `harness-*/sandbox_batch_finished`。Evolution compatibility
adapter 保留既有 `evo-*-cohort` identity、`cohort_*` error code 与 `cohort_finished` 撤销原因，避免破坏在途恢复。

## 验收证据

- adversarial coordinator fixture 在第 2 个 sample 抛错后保留 Store-confirmed `[0]` 前缀，发送防篡改
  failed checkpoint，撤销 grant 并释放 lease；
- 新 owner/epoch/grant 从 `[0]` 恢复到 `[0..4]`，每个 executing checkpoint 只报告 Store 已确认的 digest；
- progress observer 每次抛错仍完成 5/5，不影响 authority cleanup；
- naive clock、非法 token、receipt/index 错位在 Runtime lease 或 sample 执行前失败；
- 真实 HarnessStore + Interventional RED→GREEN cohort 保持中断恢复、candidate overlay、H5a/H5c/Attribution
  全链通过；
- Ruff、编译和定向测试通过，未运行全量测试。

## 当前边界与下一步

Coordinator 当前按序执行 sample；尚未接入通用 `/harness eval sandbox` Service/Tool/UI surface，也未实现跨
Batch admission/backpressure 或 Linux/Windows CI。EVO-03.6a/6b 已冻结 Probe/Profile/check、RED/GREEN
平台矩阵、samples、lanes 和最坏预算；EVO-03.6c 已提供消费外部 Batch authority 的单 lane/sample executor，
并真实写入 H5a。下一步只需提供 adversarial lane adapter，把 coordinator 的 grant/连续前缀交给该 executor，
由本 coordinator 统一恢复和清理；不得新建 Evolution worker/cohort 状态机。
