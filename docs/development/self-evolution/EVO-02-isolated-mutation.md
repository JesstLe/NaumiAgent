# EVO-02 隔离变异与补丁生成

## 目标

所有自我修改在专用临时 worktree/沙箱中进行，保护主工作树、用户未提交改动和受保护模块；变异
以 patch artifact 表示，可审查、可复现、可丢弃。

## 子模块

- EVO-02.1 Experiment contract：candidate、baseline commit、scope、budget、allowed tools/checks。
- EVO-02.2 Worktree lease：唯一目录、branch、owner、expiry、cleanup/tombstone。
- EVO-02.3 Source snapshot：baseline tree/profile/config/tool versions digest。
- EVO-02.4 Mutation planner：最小变更、目标文件、测试先行、禁止无关重构。
- EVO-02.5 Patch writer：原子修改、diff size/file count/API change 限制。
- EVO-02.6 Static guard：protected modules、path escape、secret、binary、generated file。
- EVO-02.7 Mutation receipt：diff digest、files、rationale、attempt、tool evidence。

## 安全边界

- 不在当前 checkout 直接写；不使用 `git checkout` 覆盖用户改动。
- 安全、权限、更新签名、secret storage、migration runner 默认为 protected。
- bypass 只允许已授权 experiment scope，不扩大 protected scope。
- 网络默认关闭；安装依赖必须在 contract 声明并记录 lockfile 差异。

## 验收标准

- 主工作树 dirty 时 experiment 仍隔离，结束后原字节和 index 不变。
- 进程崩溃后 lease/tombstone 可清理，不误删其他 worktree。
- 越界路径、symlink escape、protected module、超文件/行数变更被机械拒绝。
- 同 baseline+candidate+seed 可重建相同 experiment manifest。
- 失败 patch 保留证据但不进入验证/推广。

## 实现进展（2026-07-18）

- EVO-02.1a 已实现不可执行 Experiment Contract v1：只有来源仍可验证的 approved Evolution Proposal
  可以签发；manifest 固化真实 Git HEAD、dirty 事实、scope、risk budget、固定工具和机械检查，并使用
  canonical digest 生成稳定 ID。
- Contract 固定要求 worktree lease/source snapshot/static guard，且 `execution_ready=false`；当前批准不会
  触发代码、Git 写入或实验资格。
- 真实 Git + Candidate SQLite + Workbench SQLite 端到端与篡改/漂移/越界预算测试已通过。详见
  `EVO-02-1a-experiment-contract.md`。
- EVO-02.2a 已实现持久 Worktree Lease v1：Contract/owner/baseline/路径唯一绑定，SQLite CAS 并发仲裁，
  精确 baseline 创建，崩溃窗口恢复、到期回收和 dirty/ahead tombstone；任何状态都不授予执行权限。
  `AgentEngine` 已组合 Contract Issuer、Lease Store 与 Lease Manager。详见
  `EVO-02-2a-worktree-lease.md`。
- EVO-02.3a 已实现不可变 Source Snapshot v1：精确 Git tree、Harness Profile、Contract 安全配置和
  Runtime Tool Registry identity 均进入防篡改摘要；dirty/branch/path/Profile/tool 漂移 fail-closed，且
  仍固定 `execution_ready=false`。详见 `EVO-02-3a-source-snapshot.md`。
- EVO-02.4a 已实现不可执行 Mutation Plan v1：Candidate/Contract/Lease/Snapshot 四重绑定，真实
  baseline blob 扫描，固定 inspect→RED→mutation→guard→GREEN→receipt 顺序，按文件/指标收紧预算，
  并禁止 scope expansion、无关重构、网络与依赖安装。详见 `EVO-02-4a-mutation-plan.md`。
- EVO-02.4b 已实现显式多文件 Scope：`files:path-a,path-b` 从 Feedback intake 经 Proposal/Contract
  一致性校验传播到真实双文件 Plan 与 Static Guard。详见 `EVO-02-4b-multi-file-scope.md`。
- EVO-02.6a 已前置实现 Static Guard Preflight v1：具体提议内容在写入前经过 protected、dependency、
  path/symlink、generated/binary、hardcoded secret 与收紧预算门禁；Receipt 与内容摘要绑定，bypass
  不可覆盖且仍不授权写入。详见 `EVO-02-6a-static-guard-preflight.md`。
- EVO-02.5a 已实现 Guard-bound 单文件原子 Patch Writer：Lease 互斥、fresh Receipt 等值复核、同目录
  原子替换、摘要/Git scope postflight 和失败字节回滚；仍固定 `execution_ready=false`。详见
  `EVO-02-5a-single-file-patch-writer.md`。
- EVO-02.5b 已实现持久 intent journal 与崩溃恢复：prepared/replaced/committed/rollback CAS、backup
  digest、dead owner/orphan lock 回收、启动恢复，以及 TUI/新 UI 状态闭环。详见
  `EVO-02-5b-patch-journal-recovery.md`。
- EVO-02.5c1 已实现不可执行多文件 write-set journal contract：一次 SQLite 事务保存全部文件事实与
  baseline backups，按 Guard 顺序 apply、严格逆序 rollback，支持 CAS、篡改检测、预算内 revised Guard
  重试和并发 Lease 收敛；它不写文件且不授予执行权限。详见
  `EVO-02-5c1-write-set-journal.md`。
- EVO-02.5c2a 已实现 Guard-bound 多文件 Writer：完整 preflight 复核、首写前全集 intent、逐文件 CAS、
  精确 Git scope postflight、普通异常严格逆序回滚、持久回执 replay，以及单/多事务双向互斥。详见
  `EVO-02-5c2a-multi-file-patch-writer.md`。
- EVO-02.5c2b 已实现多文件启动恢复：全组 before/after/unknown 判定、严格逆序 cursor、活动锁延后、
  单/多 journal 冲突、Engine 启动顺序，以及 TUI/新 UI 分类状态。详见
  `EVO-02-5c2b-write-set-recovery.md`。
- EVO-02.6b 已实现写后完整 Diff/API Guard：从精确 Git baseline 和落盘字节重建 diff、mode 与 Python
  公共 API 事实；breaking API、不支持的源码 parser、范围或摘要漂移都会触发单文件/整组回滚，Writer
  Receipt v2 内嵌防篡改证据并兼容历史 v1。详见 `EVO-02-6b-postflight-diff-api-guard.md`。
- EVO-02.7a 已实现不可变 Mutation Receipt Core：只从 committed Journal/Patch Set、Writer v2 与重算
  Postflight 生成，持久化 attempt、diff/API、approved rationale、required metrics 和治理工具证据；并发
  幂等、篡改/漂移/secret fail-closed。详见 `EVO-02-7a-mutation-receipt-core.md`。
- EVO-02.7b1 已实现隔离内存 Mutation Generation：直接执行 Plan 允许的原始 `ToolCall`，不写磁盘，
  记录 call 顺序、失败/重试、参数/result 与逐文件 before/after digest chain，并持久化不可变 Trace。
  详见 `EVO-02-7b1-mutation-generation-trace.md`。
- EVO-02.7b2 已把同 attempt Trace 强绑定到 Static Guard v2、Writer v3 和 Mutation Receipt v2，并保留
  历史 artifact 读取边界，详见 `EVO-02-7b2-trace-receipt-binding.md`；专用 Mutation Turn Runner 与
  HAR-08 RED/GREEN 尚未串联，因此 EVO-02 整体保持 partial。
