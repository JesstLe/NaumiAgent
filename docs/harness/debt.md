# Harness 已知债务与后续阶段

本文件只记录真实缺口，不创建未使用的空壳模块。

## 尚未实现

| 阶段 | 缺口 | 完成证据要求 |
|---|---|---|
| H3 | Completion Contract、allowlisted Check Runner、一次纠正 Gate | 变更任务缺少当前 tree fingerprint 检查时不能 verified；进程取消/timeout 有真实测试 |
| H4 | Evidence Store、artifact、replay | SQLite/文件损坏、脱敏、trace 丢失、重放一致性通过 |
| H5 | Static/Replay/Live Eval 与 baseline | 离线确定性重跑一致；live 显式预算和 Worktree |
| H6 | Failure fingerprint、Proposal、人工 promotion | 无自动改 Profile；去重和阈值可审计 |
| H7 | Mission/Issue/Lease 常驻控制面 | H1-H6 baseline 稳定后再接入，恢复/取消/dirty Worktree 完整 |

## H2 当前限制

- 相关性是确定性启发式，不理解语义同义词；任务中写出路径、类名或符号能显著提高召回。
- 非 Git 目录无法可靠发现“新增但未进入旧候选集”的文件；已选择证据的 bytes 仍会精确校验。
- L1 使用有界文件证据块，尚未构建 AST 级符号索引；大文件依赖 Profile 大小上限与 L2 精确读取。
- 文档新鲜度目前由本索引的记录和人工/测试证据维护；`harness doctor` 的 `possibly_stale` 自动判断尚未实现。
- 缓存是进程内缓存，应用重启后重建；这避免了磁盘索引迁移和损坏恢复问题，但首次请求更慢。
- NaumiAgent 写工具成功后会立即失效缓存；外部编辑器新增未跟踪文件最多等待 30 秒 Git 审计周期才进入候选集。
- Windows 的路径归一化和 argv 行为有单元边界，仍需要 Windows CI 的真实 Git/NTFS 验证。

## 当前不应做的事情

- 不为了提高召回率直接接入 ChromaDB 或远程 embedding。
- 不让 `harness_read_knowledge` 绕开 Profile include/exclude 读取任意工作区文件。
- 不把 Trust Store 搬进 `.naumi/` 或提交到 Git。
- 不在 Terminal UI、Mac Workbench 各自维护另一套索引。
- 不提前把 H3-H7 类名注册进 Engine 造成“看起来已经实现”的假象。
