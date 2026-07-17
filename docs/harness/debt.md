# Harness 已知债务与后续阶段

本文件只记录真实缺口，不创建未使用的空壳模块。

## 部分实现

| 阶段 | 缺口 | 完成证据要求 |
|---|---|---|
| H5 | hello Static Eval、Baseline Identity、身份/机械/策略比较器已实现；通用 Static、Replay/Sandbox/Live runner、统计比较、baseline promotion/store/UI 未实现 | 多领域离线确定性重跑一致；live 显式预算和 Worktree |

## 尚未实现

| 阶段 | 缺口 | 完成证据要求 |
|---|---|---|
| H6 | Failure fingerprint、Proposal、人工 promotion | 无自动改 Profile；去重和阈值可审计 |
| H7 | Mission/Issue/Lease 常驻控制面 | H1-H6 baseline 稳定后再接入，恢复/取消/dirty Worktree 完整 |

## 当前限制

- 相关性是确定性启发式，不理解语义同义词；任务中写出路径、类名或符号能显著提高召回。
- 非 Git 目录无法可靠发现“新增但未进入旧候选集”的文件；已选择证据的 bytes 仍会精确校验。
- L1 使用有界文件证据块，尚未构建 AST 级符号索引；大文件依赖 Profile 大小上限与 L2 精确读取。
- 文档新鲜度目前由本索引的记录和人工/测试证据维护；`harness doctor` 的 `possibly_stale` 自动判断尚未实现。
- 缓存是进程内缓存，应用重启后重建；这避免了磁盘索引迁移和损坏恢复问题，但首次请求更慢。
- NaumiAgent 写工具成功后会立即失效缓存；外部编辑器新增未跟踪文件最多等待 30 秒 Git 审计周期才进入候选集。
- Windows 的路径归一化和 argv 行为有单元边界，仍需要 Windows CI 的真实 Git/NTFS 验证。
- CheckRunner 的 success cache 仍只在当前进程复用；持久 Store 用于证据、Explain 和 Replay，
  尚不把历史检查当作跨重启可复用缓存。
- Eval 结果仍只在内存中返回；当前已建立可审计的 Baseline Identity 与跨 commit
  compatibility/mechanical/policy comparator，但尚未建立统计比较、人工 promotion 或
  `harness_eval_results` 持久表。
- 离线 Suite/expected 随 Profile 一起由仓库维护，当前只有 digest integrity，没有 baseline
  promotion 审批；修改预期仍需代码审查，不能据此宣称模型或 Harness 已提升。

## 当前不应做的事情

- 不为了提高召回率直接接入 ChromaDB 或远程 embedding。
- 不让 `harness_read_knowledge` 绕开 Profile include/exclude 读取任意工作区文件。
- 不把 Trust Store 搬进 `.naumi/` 或提交到 Git。
- 不在 Terminal UI、Mac Workbench 各自维护另一套索引。
- 不把 H3 的进程内 Receipt 宣称为 H4 持久证据；跨重启解释必须等待 Evidence Store。
