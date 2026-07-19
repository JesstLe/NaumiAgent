# EVO-03.3c2b2 Interventional GREEN Single Sample

## 目标

在不执行 candidate 主 worktree、不复制未授权改动的前提下，用与 RED 完全相同的 Profile、Suite、seed、
configuration 和平台身份，通过 ARC-04 Worker 执行一个 candidate GREEN sample，并把 Profile-check 与
typed metric evidence 原子写入既有 HAR-08 H5a Store。

## Authority 与执行顺序

1. 防篡改地重验 GREEN Request、完整 RED cohort receipt、Validation Plan、Profile binding、Metric binding
   与 Experiment Lease；GREEN Request 必须等于当前 authority 重新构造的确定性结果。
2. 从 H5a 重载全部连续 RED sample，逐项核对 ordered result digest、精确 baseline source、共同
   configuration、cohort-scoped Run Grant、ARC-04 lifecycle 与 metric observation。
3. 重读 Lease Store，要求传入 Lease 仍是当前 active、未过期记录；候选必须位于受管 worktree storage，
   HEAD/branch/status/path/blob digest/executable mode 必须与 Lease + Plan 完全一致。
4. typed candidate blobs 一对一转换为 `HarnessSandboxSourceOverlay`。Sandbox Runner 从 baseline commit/tree
   物化完整源码后覆盖授权文件；未授权 dirty path、暂存、rename、symlink、特殊文件与额外文件一律阻断。
5. 复用 RED sample kernel 获取 Runtime lease/Run Grant，逐项走生产 Shell admission 与 ARC-04 lifecycle。
   Profile trust 与 Candidate Snapshot 在 Worker 前后及 H5a 持久化前重新验证。
6. 在独立临时目录中用相同 `self_review_static@1` 运行 GREEN metric；与 checks 合成同 suite SHA 和
   configuration，仅 source identity 变为 candidate fingerprint 且 `dirty=true`。
7. 生成防篡改 `EvolutionInterventionalGreenSampleReceipt`，绑定 GREEN/RED/Plan/Profile/Lease/Candidate、
   sample seed、H5a result、candidate identity、check status 和 lifecycle digest。

## 幂等与故障语义

- H5a 已存在时仍先完成 authority、RED cohort、Lease 与 Candidate Snapshot 重验；候选发生漂移时不得返回
  旧回执。
- 已有 result 必须精确匹配 candidate identity、共同 configuration、check/metric 集合与执行证据，否则以
  `existing_green_sample_conflict` 阻断。
- 首次执行失败不写部分 H5a；sample 自有 Run Grant/Runtime lease 在成功、失败和异常路径都撤销/释放。
- 主 workspace 与 candidate worktree 都不会被 Worker 直接执行；Worker 只执行短生命周期 baseline+overlay
  snapshot。

## 验收证据

- 真实 Git baseline + 受管 candidate worktree：RED 五样本完整落库后，GREEN sample 实际执行同一 Profile
  check；candidate 导致 check failure 时保存为 implementation failure，而不是 runner error。
- 同一 candidate 的静态 metric 从 RED `0` 变为 GREEN `1`，证明不是 Prompt 套壳或假回执。
- 重复调用不需要新的父权限即可返回同一回执，但 candidate 文件漂移后立即阻断。
- 篡改 GREEN Request 在读取 Lease、获取 Runtime lease 或签发 Run Grant 前失败。
- baseline workspace 内容保持不变，ARC-04 lifecycle、Run Grant 清理和 H5a evidence 完整。

## 后续依赖

EVO-03.3c2c1 已把 RED 验证过的 cohort-scoped Runtime lease/Run Grant、连续前缀恢复与清理编排抽为
RED/GREEN 共用治理内核；EVO-03.3c2c2 已复用该内核交付连续 GREEN samples、中断前缀恢复和 completion
receipt。下一步接 interventional comparator，不得直接跳到自动晋升或自修改闭环。
