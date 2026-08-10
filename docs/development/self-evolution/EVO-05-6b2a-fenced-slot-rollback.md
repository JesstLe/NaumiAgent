# EVO-05.6b2a Fenced Installed-Slot Rollback

## 目标

把 current `Rollback Request → Immutable Rollback Source` 安全链连接到 ARC-07 installed-version slot，执行一次
可对账、可恢复的 binary bundle 原子回滚，并验证回滚后的稳定 launcher resolution。

本切片完整支持 `data_restore_required=false` 的发行槽回滚；需要配置或数据快照恢复的请求必须失败关闭，等待
ARC-07.6 提供 exact snapshot/migration authority。它不覆盖开发 workspace、不执行 Git checkout，也不把回滚事实解释为
HAR-09 Outcome 或 promotion。

## 最小 ARC 前置

ARC-07.5a Active Pointer 新增独立 v3 rollback authority：

- `kind=evolution_revalidation_rollback_source`；
- ID 必须是 exact `evrerollbacksrc_*`；
- digest 必须是 immutable Rollback Source SHA-256；
- 只允许 `action=rollback`，与 v2 activation authority 互斥。

普通 `ReleaseSlotStore.rollback()` 继续产生兼容的 v1 event；v2 activate 行为不变。authority-bound 调用若目标 slot
已经 current，但 current pointer 没有绑定同一 authority，必须返回 conflict，不能把他人的切换冒认为本次执行。

## 执行门禁

`EvolutionRevalidationRollbackExecutionService.execute(request_id=...)` 只接受严格 ID，并在任何指针修改前重新读取：

1. exact durable Rollback Request、Source 与 Rollout Plan；
2. Source 的 content-addressed baseline blobs，逐个复算大小和 SHA-256；
3. Request 绑定且仍 current 的 HMAC-attested paused kill switch；
4. current slot 的 commit/tree 必须等于 Plan target；
5. previous slot 的 commit/tree 必须同时等于 Source 与 Rollback Plan baseline；
6. current pointer 必须仍是上述 candidate→baseline pair；
7. `data_restore_required` 必须为 false。

随后对 baseline slot 重新执行真实 `--version` boot probe，再次读取全部 authority 并重验 pointer，最后以
expected-pointer SHA-256 CAS 写入 v3 rollback event。调用方不能指定 slot、generation、Boot Receipt、时间或 authority 内容。

## 崩溃恢复与并发

- Release pointer 与 append-only event 在 ARC-07 SQLite FULL-synchronous 事务内原子提交；
- pointer 已切换但 Evolution Receipt 尚未写入时，重试按 exact Source authority 搜索完整 activation chain；
- recovered event 必须是 source-bound v3 rollback，且 previous generation、candidate/baseline provenance 完全匹配；
- 回滚后的 Boot Receipt 从 pointer 自身重读，不信任竞争者在 CAS 前获得的本地返回值；
- launcher resolution 使用 rollback activation timestamp，确保多实例恢复产生相同 content identity；
- 同一 Request 的 durable Store 以 `request_id/source_id` 唯一，冲突内容拒绝覆盖。

该设计避免崩溃恢复再次调用普通 rollback 而把 baseline 反向切回失败 candidate。

## Receipt 与动态 authority

Receipt 冻结 Request/Source/Plan 引用、candidate/baseline slot、expected/rollback pointer、fresh Boot Receipt 和
post-rollback Launch Resolution，并明确：

- `rollback_executed=true`、`release_pointer_switched=true`；
- `process_started=false`、`user_process_started=false`；
- `workspace_write_executed=false`、`git_write_executed=false`；
- `outcome_recorded=false`、`promotion_authority=false`。

动态 View 重新验证 durable rows、Source blobs、control history、完整 release history、baseline bytes、Boot Receipt 与
Launch Resolution。`rollback_fact_authority` 表示历史事实仍可证明；只有 rollback pointer 仍是 tail 时才设置
`active_baseline_authority=true`。后续 activation 不抹除历史事实，但不会继续声称该 baseline 当前生效。

## 聚焦验收

- 真实临时 Git 仓库形成 baseline/candidate commit 与 exact tree digest；
- 两个真实闭源 bundle 完成 install、boot、baseline activate、candidate activate；
- 注入“pointer 已提交、Receipt 首次写入失败”，重试从 authority-bound history 恢复；
- 八个独立 Service 并发重试收敛到一个 generation、一个 Receipt 和一个 Launch Resolution；
- 回滚不修改 workspace candidate 文件或 Git；
- Source durable JSON 篡改后动态撤销 fact/active authority；
- `data_restore_required=true` 在 pointer 修改前失败关闭；
- v1 rollback、v2 activation 与 v3 rollback authority 兼容测试通过；
- 只运行相关小模块测试，不运行全量测试。

## 明确未完成

- ARC-07.6 配置 snapshot、数据备份/恢复、migration compatibility 与 forward recovery；
- Windows 真实 `.exe` rollback runner；当前 POSIX 真实启动链与跨平台 schema/target 单测不能替代 Windows 验收；
- 用户/Agent 双通道的显式 rollback action 与 New UI/TUI 进度页；当前仅完成 Engine 内部 authority composition；
- EVO-05.7 `rolled_back/superseded` Outcome 与 HAR-09.6 Proposal before/after 回注。

下一最小切片应先把本执行服务接入共享显式交互（normal 单次确认、bypass 直接执行）并显示 durable Receipt；随后
EVO-05.7 才能消费真实 rollback fact，HAR-09.6 才能关联 Proposal 实施后的 HAR-08 before/after 与最终状态。
