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
- EVO-02.3..02.7 尚未实现，因此 EVO-02 整体保持 partial。
