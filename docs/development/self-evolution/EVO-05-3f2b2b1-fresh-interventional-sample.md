# EVO-05.3f2b2b1 Fresh Interventional Sample Pair

## 1. 目标

让 current-target Fresh Evaluation 第一次真正执行，而不是继续签发纸面 authority。一个 sample 同时运行精确 RED
revision 与相同 revision 加 immutable GREEN overlay，产出两份 H5a Eval Result 和一份不可变 pair receipt。

本切片是 Interventional cohort 的最小执行单元；它不声称 cohort、comparison、attribution、Final Evaluation 或
promotion 已完成。

## 2. 权威输入

`EvolutionRevalidationInterventionalSampleExecutor` 只接受：

- 动态状态为 `ready` 的 Fresh Runtime Contract；
- 与 Contract 的 Validation Plan、Source Snapshot、suite、seed 和 samples 完全一致的 Runtime Source Pair；
- 当前仍受信任且所有 check spec/argv/timeout 均未漂移的 Harness Profile；
- 显式授权 `bash_run` 委托的父权限回执。

任一输入 stale、缺失或摘要不一致均 fail closed。旧 Candidate Lease、旧 worktree 与旧 `evvplan_*` 不参与新链路。

## 3. 真实执行

每个 phase 都通过共享 `HarnessSandboxEvalExecutionKernel` 进入 ARC-04 Worker，并保留 job、lifecycle receipt 与 Run
Grant digest。RED 从 Git revision 读取目标 blob；GREEN 从 content-addressed immutable overlay 读取 blob。两边随后调用
已绑定的 `self_review_static@1` runner 扫描真实物化文件；不调用模型生成评测结论。

RED/GREEN 共享 suite configuration、Profile 和同一次捕获的平台身份。源码身份保持不同：RED 为 clean target，GREEN
为 exact target + overlay 的 dirty composite identity。

## 4. 持久化、恢复与 fencing

- RED/GREEN 分别写入隔离 H5a batch，结果不可覆盖；
- pair receipt 事务内复验 Runtime Contract ID/digest；
- 重复调用复验 receipt 引用的 H5a，不因已有行而跳过 authority 检查；
- GREEN 中断时允许保留已完成 RED，重试只续跑缺失 phase；
- 每次恢复按 Runtime lease epoch 签发新的 Run Grant，receipt 分别记录 RED 和 GREEN grant，避免伪装成同一次授权；
- 结束时撤销 Grant 并释放 Runtime lease；source 在 receipt 落盘前再次复验。

## 5. 验收标准

- 真实 RED/GREEN 文件均进入受绑定 metric runner；
- Profile checks 通过共享 ARC-04 kernel 运行，且每项都有 lifecycle evidence；
- 两份 H5a 具有相同 configuration/platform、不同 source identity；
- stale Contract/Profile/source、非法 sample index、缺父授权与损坏 H5a 均阻断；
- RED 已完成而 GREEN 中断后可连续恢复，phase grant 不混淆；
- receipt 明确 `cohort_complete=false`、`promotion_authority=false`；
- engine composition 和 lazy module exports 完整；
- 仅执行本模块及相邻 runtime/source tests，不执行全量测试。

## 6. 后续

EVO-05.3f2b2b2 将用该 sample executor 构造连续 `0..N-1` cohort，加入共享 batch admission、恢复前缀复验、
总预算与 cohort receipt；随后才允许 EVO-05.3f2b2b3 生成 paired comparison。Adversarial lane 仍由
EVO-05.3f2b2c 单独实现。
