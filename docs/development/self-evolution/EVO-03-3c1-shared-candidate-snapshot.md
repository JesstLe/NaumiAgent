# EVO-03.3c1 Shared Candidate Worktree Snapshot

## 目标

把 EVO-03.3a 静态 GREEN 中已经真实运行的 candidate worktree 捕获逻辑提升为唯一公共边界，供静态与
interventional GREEN 共用。该切片不执行 Profile check、不签发 Run Grant、不写 H5a；它只证明交给后续
runner 的候选字节来自当前受管 Lease、精确 Validation Plan 和稳定 worktree 状态。

## 公共合同

`capture_candidate_worktree_snapshot()` 接受 `ExperimentWorktreeLease`、Validation Plan v2 和受管
worktree storage，重新验证嵌套 artifact 后捕获：

- Lease/Contract/manifest/baseline 与 Plan 完整一致，Lease 仍 `worktree_ready` 且不直接授予执行权；
- 调用方必须提供带时区时钟，Lease 必须仍为 active 且未过期；
- worktree 必须是受管 storage 的直接子目录，真实目录名匹配 Lease；
- Git top-level、HEAD、branch 精确匹配 Lease；
- porcelain status 只能包含 Plan 指定路径，modify 必须为未暂存 ` M`，create 必须为 `??`；
- rename/copy、staged、extra、missing、symlink、非普通文件和超过 2 MiB 的文件全部失败关闭；
- 每个文件字节 SHA-256 等于 Plan `candidate_sha256`；
- 读取前后 `TreeFingerprint` 完全相同。

返回的 `EvolutionCandidateWorktreeSnapshot` 只在进程内持有 root、immutable blobs 与 fingerprint，不写入
源码 artifact。`revalidate_candidate_worktree_snapshot()` 在扫描或 worker 执行之后再次核对 fingerprint，
防止捕获后、H5a 持久化前发生并发漂移。

## 迁移结果

EVO-03.3a 已删除私有 `_capture_candidate_snapshot` 和 status parser，直接消费公共快照并在扫描后复验。
原有用户可见错误 code 保持兼容；因此当前不存在静态与 interventional 两套候选解析规则。

## 验收标准

- 真实受管 Git worktree 的 modify candidate 能捕获精确字节；
- 无变化时复验通过，捕获后修改文件时返回 `candidate_worktree_changed_after_snapshot`；
- EVO-03.3a 的 modify/create、幂等、extra path、digest drift、stale Lease 回归保持通过；
- 公共 lazy export 可用，Ruff 与 focused tests 通过。

## 下一步

HAR-08.4d 已允许把同一 Snapshot blobs 作为精确 baseline revision 的受权 overlays，并在 admission 前与
Worker 完成后复验 Snapshot。EVO-03.3c2 下一步复用该入口执行与 RED 相同的 Profile check 和 metric runner；
完整 cohort 编排仍留给后续独立切片。
