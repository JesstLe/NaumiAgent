# HAR-08.4c 精确 Git Revision Sandbox Snapshot

## 1. 交付目标

让 HAR-08.4a Runner 除当前工作树外，还能从 EVO Validation authority 指定的完整 Git commit 物化一次性
Sandbox workspace。该切片解决“baseline 到底是哪组源文件”的真实性，不生成 cohort、不自动比较，也不把
静态 `protocol_hello` Suite 错误改造成 Shell Eval。

## 2. Authority 与快照合同

- 调用方必须同时提供 40/64 位完整小写 `source_revision` 与 `expected_source_tree_sha256`；禁止 branch、tag、
  abbreviated SHA 或单独提供其中一项；
- Runner 使用与 `EvolutionExperimentSourceSnapshot` 相同的
  `git ls-tree -r -z --full-tree <commit>` 原始字节计算 SHA-256，并要求完全匹配；
- `rev-parse <revision>^{commit}` 必须精确返回传入 revision，不能在执行时漂移到另一个 commit；
- 只接受 `100644/100755 blob`，symlink、gitlink/submodule、未知 mode/type、非 UTF-8 路径和路径逃逸全部
  fail closed；敏感路径规则与当前工作树快照完全相同；
- blob 先读取 Git 声明大小并受文件数/总字节上限约束，再直接流式写入私有 snapshot；落盘后通过仓库
  object format 执行 `hash-object --no-filters`，必须回到 tree 中的 object id；manifest 保存实际 size/SHA-256、
  source kind、revision 与 authority tree digest；
- 执行终态重新解析 commit/tree identity。源工作树的 dirty/untracked candidate 文件既不进入 baseline
  snapshot，也不会使不可变 revision 结果误标 stale。

## 3. Manifest v2

`.naumi-sandbox-manifest.json` 升级为 schema v2，新增：

- `source_kind`: `working_tree | git_revision`；
- `source_revision`: revision 模式为完整 commit，当前工作树模式为 `null`；
- `source_tree_sha256`: 当前工作树 fingerprint 或精确 Git tree listing SHA-256。

Shell Worker 继续只消费完整 manifest digest，因此旧 admission/ToolJob/ExecutionGrant 链无需平行 schema。

## 4. 验收证据

- 真实 Git baseline 为 `VALUE=1`，当前脏工作树为 `VALUE=99` 且含 candidate-only 文件；Check 在 Sandbox
  只观察到 baseline bytes，并读取正确 manifest revision/source kind；
- Result 返回精确 `source_revision` 与 `source_tree_sha256`，artifact/lifecycle 正常，执行后 snapshot 清空；
- 错误 tree digest、revision/tree 单边参数在 admission 前拒绝；
- 通过 Git index 构造的 symlink tree entry 在目录创建和 admission 前拒绝；
- 当前工作树 Sandbox、安全敏感路径与真实 macOS worker 回归保持通过。

## 5. 诚实边界与下一步

- 该 workspace 由 Git object bytes 物化，不携带 `.git` metadata；ruff/pytest/compile 等源码 Check 可运行，
  但需要 `git status/log/diff` 的 Check 仍须未来的受控 Git metadata broker，不能声称完整 ephemeral worktree；
- 还没有把 `EvolutionBaselineCohortRequest` 的 check 顺序、seed、样本预算和 H5a Result 组合成 executor；
- 下一最小切片应在 EVO-03 新建 interventional RED sample executor：重新验证 Contract/Plan/Binding/Request，
  对每个 sample 顺序调用本 Runner，并生成既有 HAR-08 typed evidence；不得另造 subprocess 或结果 Store；
- Candidate GREEN 还需要从受管 Lease revision/patch 生成等价隔离 snapshot，之后才能形成成对 cohort。
