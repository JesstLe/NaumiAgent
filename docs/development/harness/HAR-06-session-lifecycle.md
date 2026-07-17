# HAR-06 Session 生命周期与派生记录治理

## 目标

Session 的 archive/delete/retention 与 Harness 派生记录保持一致，既不留下不可解释孤儿，
也不误删用户要求保留的审计证据。

## 子模块

- HAR-06.1 Lifecycle policy：`retain|archive|delete|legal_hold` 四种策略。
  - **已实现**：封闭策略/操作者枚举、失败关闭转换决策、legal hold 审计门、Session 状态适配器。
- HAR-06.2 Reconciliation hook：Session 操作成功后触发幂等 Harness 清理。
  - **HAR-06.2a 已实现**：持久化 `prepared -> session_committed -> records_committed`
    状态机、Artifact 引用快照、作用域隔离与原子 Harness 行清理。
  - **HAR-06.2b 已实现**：独立于 Engine 的协调器按持久状态执行 Session/Harness 两阶段删除，
    并以确定性 request id 支持跨进程恢复。
  - **HAR-06.2c 已实现**：Engine 删除入口、CLI/New UI/TUI/Agent Tool/API 和启动恢复均接入
    同一协调器；Session 已删除即撤销运行时会话权限，Harness 失败则返回持久重试状态。
- HAR-06.3 Tombstone：删除失败时记录安全 tombstone，后台重试。
  - **HAR-06.3a 已实现**：封闭失败阶段/错误码、事件幂等、确定性退避、重试上限、
    并发 worker 租约、过期接管和 resolved 审计状态。
  - **HAR-06.3b 已实现**：恢复协调器发现无 tombstone 的 crash gap、领取到期任务、从精确状态
    续跑并安全解决或再次记录失败；CLI/TUI/API 启动接入已完成，周期 worker 仍待 6.5。
- HAR-06.4 Artifact GC：只删除没有其他引用且通过 workspace/session 校验的 artifact。
  - **已实现**：类型化引用归一化、受管根目录限制、别名去重、存活引用复核、符号链接/目录/
    越界失败关闭、真实文件删除、持久结果计数、v4 升级回填和独立 `artifact_gc` tombstone 阶段。
- HAR-06.5 Retention worker：按天数、空间上限和最近访问执行有界清理。
- HAR-06.6 User surfaces：删除预览显示将影响的 run/evidence/artifact 数量。
  - **HAR-06.6a 已实现**：CLI/New UI/TUI 与只读 Agent Tool 共用 workspace-scoped
    删除影响预览；Artifact 只展示引用数，不冒充可删除文件数。

## 当前进度（2026-07-17）

HAR-06 当前为 `partial (6.1, 6.2a-6.2c, 6.3a-6.4, 6.6a)`。已落地的前置能力包括：

- `harness.retention` 定义无副作用的生命周期决策；`legal_hold` 阻断后台转换，唯一自动
  状态变化为 `archive -> delete`，且只有 `delete` 策略允许自动清理。
- 现有 Session Store 的 `active/archived` 通过失败关闭的单一适配器映射到
  `retain/archive`，为 HAR-06.2 提供稳定边界。
- Harness DB v3 保存不可变协调作用域和类型化 Artifact 引用；只有 Session 删除被确认后，
  才能在单一 SQLite 事务中级联删除精确 workspace/session 的 Harness 行并推进状态。
- 相同 request id 可跨进程幂等重放；作用域或 actor 不同会触发冲突，损坏记录失败关闭。
- Harness DB v4 用不含异常原文的 tombstone 保存失败阶段、封闭错误码与重试时间；failure id
  防止重复计数，租约保证并发 worker 只有一个执行者，过期任务可被其他进程接管。
- Harness DB v5 为每个协调请求保存 `pending|completed` Artifact GC 状态及候选、删除、缺失、
  共享、风险与非普通文件计数；升级时为历史协调记录回填 pending，使旧 `records_committed`
  请求也能由启动恢复补做 GC。tombstone 支持独立 `artifact_gc` 阶段。
- `ArtifactGarbageCollector` 只管理工作区 `artifacts/` 与 `.naumi/artifacts/`；Check 路径和
  `artifact://` URI 归一为同一物理目标。数据库写事务内以 256 行批次扫描其他 Session 的
  存活引用，锁住并发新引用后再删除无共享普通文件。
- POSIX 删除逐级使用 `dir_fd` 与 `O_NOFOLLOW`，Windows 使用严格 resolve/lstat 复核；路径穿越、
  符号链接、目录、设备文件、受管根目录外文件和无法可靠解释的存活引用一律保留。
- `SessionReconciliationCoordinator` 只依赖最小 Session `load/delete` 协议；正常路径与
  Session/Harness 两阶段故障均可恢复，取消会先持久化安全 tombstone 再向调用方传播。
- 已存在 tombstone 时直接用户重试不会绕过租约；crash gap 可被有界扫描发现并纳入重试。
- `HarnessStore.preview_session_delete()` 使用 SQL 聚合统计精确 workspace + session 的
  Run、Criterion、Check、Evidence、Replay Baseline 与 Artifact 引用，不加载记录正文。
- 历史的 Harness 删除原语现已强制提供 workspace，消除了相同 session ID 跨工作区误删风险。
- `AgentEngine.preview_session_delete()` 以 Session 保存的 workspace 为权威作用域；仅对缺失旧元数据
  回退当前工作区。
- `/history delete-preview <ID>` 已同步 CLI/New UI/TUI；Agent 可用只读
  `session_history(action="delete_preview")` 获取相同结果。
- `/delete`、历史面板删除、Agent Tool 与 API DELETE 现均经由协调器执行；API 对完整成功、
  安全重试与重试耗尽分别返回 204、202 与 503，终端界面显示对应中文状态。
- CLI、任务模式、TUI 与 API 启动时执行一次有界恢复扫描。Session Store 是运行时权限的权威：
  即使 Harness 行清理等待重试，已删除 Session 的当前会话、工作区授权和临时运行态也会撤销。
- 兼容接口 `AgentEngine.delete_session()` 仅在完整协调完成时返回 `True`；生产用户界面使用
  `delete_session_detailed()`，不会把“已进入安全重试”误报为删除失败或完整成功。

尚未实现 HAR-06.5。当前 `/delete` 已协调 Session、Harness 数据库行和安全可删除的受管 Artifact；
共享、风险、非普通文件及受管根目录外引用会保留，并在协调记录中计数。10k 级分批提交、空间上限、
周期调度、主动取消和观测指标仍由 retention worker 完成，详见 `HAR-06-4-artifact-gc-design.md`。

## 数据规则

- 外键级联只处理数据库行；外部 artifact 必须引用计数后清理。
- `legal_hold` 永不被自动 GC；解除必须是用户操作并写审计。
- tombstone 不包含 objective 或敏感摘要，只含 id、策略、重试状态和时间。
- 同一个 delete request 重试不得多删其他 Session 数据。

## 验收标准

- 归档 Session 后 `/harness explain` 仍可用，默认 latest 可配置是否包含归档。
- 删除 Session 后关联 Run/Check/Evidence 行为 0，其他 Session 行数不变。
- 模拟 artifact 删除中断后，重启 worker 可从 tombstone 恢复。
- 跨 workspace 相同 session-like id 不会互相影响。
- 10k runs 清理过程内存有界、可取消、每批提交，失败不锁死数据库。
- A3：真实 Session Store + Harness Store + artifact 目录完成预览、删除、崩溃恢复。

## 建议测试

`tests/unit/test_harness_retention.py`、`tests/integration/test_session_harness_reconciliation.py`。
