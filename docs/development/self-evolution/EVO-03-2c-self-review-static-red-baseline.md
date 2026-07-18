# EVO-03.2c Self-Review Static RED Baseline

## 目标

把已验证的 Validation Plan、RED Cohort Request 与 Metric Runner Binding 变成第一组真实的 EVO-03
baseline 样本：从精确 Git baseline 只读扫描 Self-Review finding，生成 HAR-08 typed quantitative Result，
并以连续 `sample_index=0..N-1` 写入 H5a。

本切片不运行项目代码、Profile check 或模型，也不创建 Git worktree。它只实现无执行副作用的
`self_review_static@1` lane；需要执行代码的验证仍由 ARC-04 隔离 worker 承担。

## Authority 与失败关闭

`EvolutionSelfReviewRedBaselineExecutor` 每次执行都会重新解析并交叉验证：

- Validation Plan、Baseline Request 与 Metric Binding 的 ID、digest、baseline commit/tree、sample 数量；
- Binding 必须整体 ready，且每个 entry 必须与 Runner Registry 重新解析出的 resolution 完全相同；
- 只接受 `self_review.<finding-code>.count`、`direction=decrease` 与非负整数 target；
- Plan 中所有路径必须声明为 Python；
- 用户级 Harness Trust Store 必须仍信任 Request 固定的 Profile digest。

任何嵌套 artifact 漂移、blocked runner、非法 count 合同、Profile trust 撤销或路径类型不安全都会在 H5a
写入前失败。

## 精确 Git baseline

执行器不读取当前工作区文件内容，而是只读访问 Git object database：

1. `rev-parse --verify <commit>^{commit}` 必须精确得到 Request commit；
2. `ls-tree -r -z --full-tree` 的原始字节 SHA-256 必须匹配 Source Snapshot 的
   `baseline_tree_sha256`；
3. Plan path 使用 literal pathspec 查询，必须是 `100644/100755 blob`；
4. symlink、submodule、missing path 与超过 2 MiB 的 blob 全部拒绝；
5. blob 写入系统临时目录后复用现有 `scan_self_review_files()` AST scanner，退出时整体清理。

因此，即使主工作区已修改，RED measurement 仍来自原始 baseline commit；执行器不会 checkout、stash、
修改 index 或改变用户工作树。

## 重复样本与 H5a

- 按 Request 实际重复扫描 5..100 次，不复制一次扫描结果伪造重复样本；
- 每个 metric 独立生成一个 Eval case，数值 observation 使用 `unit=count`；
- case status 由 direction/target 机械推导，guardrail 固定记录 `no_model/no_side_effect=passed`；
- Suite Identity 固定 Request、Binding、Plan、Profile、commit/tree、runner version 与平台事实；
- 所有扫描全部完成后才开始写库，扫描失败不会留下新 partial cohort；
- H5a 写入中断允许从完全一致的连续前缀续跑；不连续、越界或 canonical payload 冲突均拒绝；
- 完整 cohort 的幂等重试也会重新扫描并校验已有样本，不能只凭 key 存在返回成功。

## 完成回执

`EvolutionSelfReviewRedCohortReceipt` 防篡改地保存：

- Request、Binding、Plan identity；
- Suite/Batch、baseline commit/tree 与样本 Result digest；
- 每个 metric 的 finding code、direction、target 和全部 sample value；
- `source_access=git_object_database`；
- Profile trust 已重验；
- model/network/project execution/ARC-04 worker 均为 false；
- cohort 完整状态与稳定完成时间。

回执不保存源码、绝对路径、Git stderr、argv、模型输出或 secret。

## 验收证据

- 真实临时 Git 仓库的 baseline 含 1 个 broad exception，随后脏工作区删除该 finding；5 次 RED sample
  仍全部得到 count=1，且工作区状态前后不变；
- 完整 cohort 重试重新验证并返回同一 receipt；
- 注入第 3 次 H5a 写入中断后保留 `[0,1]`，下一次安全续写为 `[0,1,2,3,4]`；
- 已有错误 canonical sample 时拒绝追加；
- Profile trust 撤销、Git symlink、非 Python Plan path 全部在写库前拒绝；
- invalid Self-Review count direction/target 在 Runner Registry 阶段 blocked；
- Engine 默认组合 Executor，lazy public export 可用。

## 当前不足与下一步

- 本回执证明 RED metric cohort 完整写入，但不是 baseline/candidate Comparison Receipt；
- EVO-03.2d 已用 Validation Plan v2 绑定 `modify/create` 与 before/after digest：modify 必须匹配 baseline
  blob，create 必须在 baseline 缺失并使用受控空 fixture；历史 v1 仍保持不可执行；
- Profile checks、Tool、Harness failure rate 与任何项目代码执行仍等待 ARC-04；
- 下一最小切片应实现 **EVO-03.3a Self-Review Static GREEN Cohort**：从受管 candidate Lease 的精确
  文件状态读取同一 path、复用相同 metric/seed/order/平台合同，写入独立 GREEN batch；随后才能调用
  HAR-08.7e/8.7d 形成第一份真实 RED→GREEN 数值对照。
