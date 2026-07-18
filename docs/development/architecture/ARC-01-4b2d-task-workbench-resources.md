# ARC-01.4b2d Task 与 Workbench Resource Pair

## 目标与不可拆分原因

`TaskStore` 和 `WorkbenchStore` 当前共享 `memory.session_db_path`，并由 WorkbenchService、TaskMarket、
WorktreeManager、ReviewEvidenceCollector、Evolution governance 与 UI task snapshot 共同消费。若只迁移
其中一个，resource override 可以把任务与 mission/lease/review 写入不同数据库，破坏 task id、session
scope 和治理引用。因此本切片将二者作为一个资源对迁移，不按文件数量机械拆分。

## 路径和资源合同

- `RuntimePaths.session_db_path` 是 absolute/canonical，必须位于 `runtime_data_dir`；
- `runtime_data_dir` 由该路径父目录派生，避免 config 被重复解析；
- `RuntimeResources` 同时要求 `task_store` 与 `workbench_store`；
- bundle 构造复核两个 Store 的规范 `db_path` 完全相同，不同即中文错误并拒绝对象图；
- override 在默认资源构造前统一校验，两个显式实例保持 identity；
- TaskStore/WorkbenchStore 增加只读规范 `db_path` 属性，不暴露连接或可变配置。

两个 Store 均按操作打开短连接，没有常驻 connection/后台任务，构造不创建数据库。它们继续复用现有
表与迁移逻辑，不新增平行 schema。

## 生产装配

Composition Root 用同一个 `paths.session_db_path` 构造两个 Store，然后 Engine 将注入实例传给：

- WorktreeManager、WorkbenchService、ValidationRunner、ReviewEvidenceCollector；
- TaskMarket、task tools、todo reconciliation 和 typed task snapshot；
- Evolution proposal/experiment governance。

独立 `workbench export-audit` 命令也改用 `build_runtime_paths/resources`，不再在命令层旁路构造
WorkbenchStore。`TaskStore.scoped(session_id)` 仍可在同一数据库上创建轻量 session view；这是领域
API，不是默认资源重建。

## 验收证据

- Runtime Composition 覆盖路径、默认实例、override identity、不完整 bundle 和 split DB 拒绝；
- 架构门禁止 Engine 构造/导入两 Store，WorkbenchStore 生产构造点唯一在 Composition Root；
- TaskStore 只允许 Composition Root 与自身 `scoped()` 两个生产构造位置；
- 真实 Composition Engine 用注入 Store 创建 Task 和 Mission，并验证相同 session 与 db_path；
- WorkbenchService 内部引用与 Engine/RuntimeResources identity 完全一致；
- Task/Workbench Store 小模块继续覆盖 session isolation、legacy migration、依赖边、mission/event 与治理状态；
- 文档治理通过，不运行全量测试。

## 自我审视与未完成

本切片保留了现有共享 SQLite 架构，并用运行时 invariant 防止 override 拆库；没有创建第二套 Task API，
也没有把 UI snapshot 当作状态权威。

仍未完成 Goal/Pursuit、Background/Scheduler、Browser/Daemon、Worktree 资源及统一生命周期；Task 与
Workbench 的长期 Service 构造仍位于 Engine，属于 ARC-01.4c。ARC-02 command/task API、lease owner、
cursor/resume 和 UI-11 全屏导航也尚未实现，因此相关模块继续保持 partial/planned。
