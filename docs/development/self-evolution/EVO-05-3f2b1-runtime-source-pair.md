# EVO-05.3f2b1 Shared Runtime RED/GREEN Source Pair

## 1. 目标

把 EVO-05.3f2a 的不可执行 Validation Plan 转换为 Harness Sandbox Eval 可直接消费的共享 runtime source pair，消除
Interventional 与 Adversarial 各自从旧 Candidate Lease/worktree 捕获 GREEN 的重复路径。

## 2. 物化规则

`EvolutionRevalidationRuntimeSourceService.materialize()` 重读 current Validation Plan，验证 immutable Source Snapshot
与全部 content-addressed blobs，然后生成：

- RED：current target revision/tree，不带 overlay；
- GREEN：同一 revision/tree，只挂载 immutable overlay；
- RED identity：clean current target；
- GREEN identity：dirty composite tree，digest 同时绑定 target tree 与 overlay source；
- 两个 source 共用动态 `source_is_current`，每次执行前后重读 Plan、Snapshot 并复验 blob bytes。

Plan stale、工作区变化、Snapshot ID/digest、文件 path/digest/mode 或 blob bytes 任一漂移均 fail closed。该服务不读取
Candidate Lease，不要求旧 worktree 存活，不运行项目代码，也不写用户工作区。

## 3. 验收标准

- RED/GREEN revision 与 revision tree 完全相同；
- RED 没有 overlay，GREEN 精确使用 05.3f1 immutable overlays；
- GREEN composite identity 同时绑定 target tree 与 overlay digest；
- Plan stale 或 blob 篡改后，已有 source callback 立即返回 false；
- engine composition 提供单一共享 Service；
- 聚焦测试使用真实 Git/source capture/blob store，不运行全量测试。

## 4. 后续边界

本切片只建立共享 source pair，尚未改变旧 Interventional/Adversarial receipt schema 或执行器参数。EVO-05.3f2b2 将让
两类 sample/cohort executor 显式消费 `EvolutionRevalidationRuntimeSourcePair` 与新 Validation Plan，并移除 fresh 路径对
active Candidate Lease 的依赖；之后才执行完整 lane matrix。
