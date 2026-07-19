# EVO-03.3c2b1 Candidate Blob Mode Authority

## 目标

把 Candidate Snapshot 从“只有 path/bytes”升级为同时冻结 SHA-256 与 executable mode，确保 HAR-08.4d
overlay 不会在字节正确时悄悄改变脚本执行语义。本切片不执行 GREEN sample。

## Mode 规则

`EvolutionCandidateSourceBlob` 固化 `path/content/sha256/executable`，构造时重新计算内容摘要。

- modify：使用 `git ls-files --stage -z -- <path>` 读取精确 baseline stage-0 mode，只接受
  `100644/100755`；
- create：baseline index 必须没有该 path，且新文件固定不可执行；
- POSIX：candidate worktree 实际 owner-executable bit 必须与上述 authority 一致；
- Windows：不依赖本地 POSIX mode bit，但仍保留 Git baseline mode 供跨平台 Sandbox 物化；
- symlink、gitlink、非 stage-0、重复或缺失 index entry 全部失败关闭。

## 迁移与验收

- 静态 GREEN 把 typed blobs 显式投影为既有 `(path, bytes)` scanner 输入，不复制读取逻辑；
- 普通 modify 捕获 `executable=false`，内容和 Plan digest 完整一致；
- chmod 为 executable 后返回 `candidate_file_mode_mismatch`，恢复 mode 后可继续复验；
- create/modify、状态漂移、摘要漂移、Lease 过期与捕获后变更回归保持通过；
- 公共 lazy export 提供 `EvolutionCandidateSourceBlob`。

## 下一步

EVO-03.3c2b2 将 typed blobs 一对一转换为 `HarnessSandboxSourceOverlay`，通过共用 sample kernel 执行
interventional GREEN Profile checks 与 metric，并生成单 sample receipt。
