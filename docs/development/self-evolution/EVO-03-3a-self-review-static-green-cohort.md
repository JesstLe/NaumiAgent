# EVO-03.3a Self-Review Static GREEN Cohort

## 目标

在不运行 candidate 项目代码的前提下，从一个当前有效、受管的 Experiment Lease 读取精确候选文件，复用
EVO-03.2c 的 Self-Review 静态 Eval runtime，生成与 RED 相同 metric、seed、顺序、样本数和平台身份的
GREEN cohort，并写入独立 H5a batch。

本切片完成第一个真实 candidate measurement，但不自行宣布改进或推广。统计判定和最终 Evaluation Receipt
仍属于 EVO-03.4/3.7。

## Authority 链

`EvolutionSelfReviewGreenCohortRequestBuilder` 防篡改绑定：

- RED Baseline Request 与已完成 RED receipt；
- Metric Runner Binding 与 Validation Plan v2；
- candidate ID/revision、文件 after digest；
- active Lease ID、受管 worktree 名称和 branch；
- 与 RED 完全相同的 suite、sample seeds 与 requested sample count；
- 独立 `evo:green:<plan-digest>` batch。

Executor 不信任调用者传入的 Lease 快照。执行前会从 `EvolutionExperimentLeaseStore` 重新读取当前记录，要求
仍为 active、未过期、`worktree_ready=true`、`execution_ready=false`，并重新验证 Harness Profile trust。
主 workspace 也必须精确指向 Git repository root。

## Candidate 状态与不可变快照

GREEN 只接受一个最小、精确的脏 worktree：

1. worktree 必须位于配置的受管 storage 目录，名称与 Lease 一致；
2. Git root、HEAD 和 branch 必须与 Lease/Plan 一致；
3. `git status --porcelain -z` 必须恰好等于 Plan 文件集合；
4. modify 只能是未暂存的 ` M`，create 只能是未跟踪的 `??`；
5. extra、missing、staged、rename/copy、symlink、非普通文件全部失败关闭；
6. 每个文件不超过 2 MiB，内容 SHA-256 必须等于 Plan `candidate_sha256`；
7. 文件读取前后和扫描完成后重复计算 worktree fingerprint，任何并发漂移都会拒绝写入。

可信字节被复制到系统临时目录后才交给 AST scanner。扫描不读取 candidate worktree 的后续变化，也不访问
网络、模型或项目进程。

## RED 对照与 H5a 语义

- Executor 从 H5a 重新加载完整 RED batch，并逐项核对 RED receipt result digest；
- RED configuration、baseline commit/tree 与当前平台必须一致；
- RED/GREEN 共用 `self_review_static@1` case/metric/guardrail 构造逻辑，避免双实现漂移；
- 每个 GREEN 样本都真实重复扫描，不复制单次结果；
- 只有全部扫描完成且 candidate fingerprint 未变后才开始写库；
- 相同连续前缀可安全续写，完整 cohort 可幂等重放；canonical payload 冲突或 sample 间隙失败关闭。

`EvolutionSelfReviewGreenCohortReceipt` 保存 request/RED/Plan/Lease/candidate identity、candidate tree digest、
样本 result digest 与每个 metric 的全部数值；不保存源码、绝对路径、secret 或 Git stderr。

## 验收证据

- 真实临时 Git worktree 中将 broad exception 从 1 修复为 0，RED/GREEN 各 5 次；HAR-08 quantitative
  comparator 得出 improved；
- create operation 使用 baseline 空 fixture，GREEN 扫描新文件且 count 保持 0；
- extra path、candidate digest 漂移和 stale Lease 均在 GREEN H5a 写入前拒绝；
- 注入第 3 次 GREEN 写入失败后，下一次从连续前缀完成 5 个样本；
- 完整 GREEN cohort 重试返回同一 receipt；
- Engine 默认组合 Request Builder 与 Executor，公共 lazy export 可用。

## 当前不足与下一步

- 这里只执行无副作用静态 scanner；ruff、compile、pytest、contract、smoke 和平台矩阵仍必须经 ARC-04
  隔离 worker，不能借用本执行器绕过进程/资源治理；
- GREEN receipt 是完整测量回执，不是 before/after 统计结论，也不授予 promotion authority；
- EVO-03.4a 已消费持久化 RED/GREEN H5a 样本，复用 HAR-08.H5b2/7d/H5c 冻结原生 Comparison
  Receipt，EVO-03.5a 已机械持久化 Failure Attribution；下一步跨查 ARC-04/EVO-03.6。
