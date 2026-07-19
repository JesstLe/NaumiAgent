# HAR-08.4d 精确 Revision Source Overlays

## 目标

让 ARC-04 Sandbox Runner 在不读取脏 candidate worktree 执行字节的前提下，从可信 baseline Git revision
物化完整项目，再原子覆盖一组已经过摘要校验的 candidate blobs。该能力是 EVO-03 interventional GREEN
单样本的 Harness 前置，不生成 Evolution Request、cohort 或 Comparison Receipt。

## Overlay Authority

`HarnessSandboxSourceOverlay` 固化规范 POSIX path、不可变 bytes、SHA-256 与 executable bit。构造时即拒绝
路径逃逸、敏感路径、非 bytes、错误摘要和非法 mode。

Runner 只在以下参数同时存在时接受 overlays：

- 完整 `source_revision` 与 baseline `expected_source_tree_sha256`；
- 按 path 排序、无重复、最多 16 项的 overlays；
- `overlay_source_sha256`，由上层可信 Candidate Snapshot fingerprint 提供；
- async `source_is_current` 复验回调。

任一参数单边出现都在 Git 物化和 admission 前拒绝。

## 物化与 TOCTOU 边界

1. 先按 HAR-08.4c 从 Git object bytes 物化精确 baseline revision；
2. overlays 只能替换普通文件或创建新文件，不能替换目录/特殊文件；
3. 每项落盘后重新计算 size/SHA-256，总量继续受 Sandbox byte budget 限制；
4. manifest `source_kind=git_revision_overlay`，文件列表记录最终实际字节，`source_tree_sha256` 绑定外部
   Candidate Snapshot fingerprint；
5. admission 前调用 `source_is_current`，漂移时不执行；
6. Worker 完成后再次复验，漂移时保留生命周期证据但结果标为 `stale`；
7. baseline revision identity 仍在终态重新解析，Sandbox 目录始终清理。

因此执行字节只来自不可变 baseline objects 与已验证 overlays。candidate worktree 即使在捕获后被修改，也不
会把未批准字节混入 Sandbox；最多使已执行的批准结果失效。

## 验收标准

- 真实 Git baseline `VALUE=1` 经 overlay 后只在 Sandbox 中成为 `VALUE=2`，并创建 candidate-only 文件；
- manifest/Result 分别绑定 `git_revision_overlay`、baseline commit 与 candidate fingerprint；
- 主 worktree 保持原字节且不出现 candidate-only 文件；
- source callback 在 admission 前和执行后各调用一次；
- admission 前 stale 时 Worker 不执行；缺少 digest/callback 时 fail-closed；
- HAR-08.4c exact revision 与 symlink 拒绝回归保持通过。

## 下一步

EVO-03.3c2 把 `EvolutionCandidateWorktreeSnapshot.blobs` 转换为 overlays，复用 RED 的 Profile checks、Run
Grant、ARC-04 lifecycle 与 typed metric 组装，形成单个 interventional GREEN H5a sample。
