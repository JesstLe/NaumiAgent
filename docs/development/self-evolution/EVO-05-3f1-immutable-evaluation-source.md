# EVO-05.3f1 Immutable Revalidation Evaluation Source

## 1. 目标

为 EVO-05.3e Fresh Evaluation Plan 捕获可在 Candidate Lease/worktree 消失后继续使用的精确评测源码。该切片解决
rebase 后源码与原 Candidate worktree 不同的问题：后续评测必须消费 `target revision + approved overlay`，不能暗中回退
到旧 Lease 内容。

## 2. 精确重新物化

`EvolutionRevalidationValidationService.materialize_current_source()` 复用 05.3c 的唯一源码重建边界：

- 重读 exact replay/rebase artifact、Request、Promotion Input 与 active Lease；
- 重新验证 Candidate bytes、baseline/current target Git blobs、三方合并结果及 executable mode；
- 重读当前 Harness Profile/check plan；
- 返回与 Validation Receipt 相同的 target tree、overlay manifest 和真实 bytes，但不再次执行 Harness checks。

Source Snapshot 必须与 Fresh Evaluation Plan 的 target、tree、overlay digest 完全一致。

## 3. Content-addressed immutable blobs

每个 overlay 文件以 SHA-256 写入 runtime data 下的 `evolution/evaluation-sources/blobs/<sha256>`：

- 单文件上限 2 MiB；
- 写入使用同目录临时文件、`fsync`、只读权限和 atomic replace；
- 读取使用 no-follow regular-file 检查、size 与 SHA-256 重验；
- symlink、截断、篡改、digest collision 或非 regular file 全部 fail closed；
- Snapshot 只保存安全 path、digest、size、executable bit 和 storage key，不重复嵌入源码。

SQLite Source Snapshot 在事务内重读 Fresh Evaluation Plan authority。Blob Store 是 content-addressed，重复捕获幂等；
捕获完成后即使原 Candidate worktree 被清理，`load_overlays()` 仍可恢复完整评测输入。

## 4. 双通道

Agent Tool：`evolution_revalidation_evaluation_source(plan_id=...)`

手动入口：`/evolution revalidation-evaluation-source <fresh-evaluation-plan-id>`

两者共用同一 Service；只写治理 DB 与 runtime content store，不写用户工作区，不运行项目代码，不授予 promotion。

## 5. 验收标准

- 捕获的 blob bytes/digest/executable 与 05.3c exact source 一致；
- 用户工作区保持原样；
- Candidate worktree 删除后 Snapshot 仍可完整加载；
- corrupt/symlink blob 被拒绝；
- stale Fresh Evaluation Plan 不可首次捕获；
- Store 拒绝不存在或 digest 不匹配的 Plan；
- Tool/Slash/权限注册完整；
- 聚焦测试覆盖真实临时 Git worktree、SQLite 和文件系统，未运行全量测试。

## 6. 后续依赖

EVO-05.3f2a 必须先签发 Revalidation Validation Plan：把 RED baseline 从原 Candidate baseline 改绑 current target，
把 GREEN source 改绑本 immutable Snapshot，并从 durable Experiment/Mutation authority 继承及重验 seed、metrics、
checks、预算和 Candidate identity。随后 EVO-05.3f2b 才能让 Interventional/Adversarial source adapter 消费新 Plan，
同时保持 ARC-04、H5a/H5c 与恢复语义。在完整 lanes、Attribution 和新 Final Evaluation 签发前，仍不开放 reapproval
或 rollout。
