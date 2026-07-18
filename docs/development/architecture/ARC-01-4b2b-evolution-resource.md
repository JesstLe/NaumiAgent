# ARC-01.4b2b Evolution Candidate Resource Ownership

## 目标

把 EVO-01 已交付的 `EvolutionCandidateStore` 纳入 ARC-01 Composition Root 的规范路径与资源所有权，
使 Harness Feedback、Evolution Review 及后续 EVO-02/03 消费同一个明确实例。本切片连接 Future
Architecture、Harness 与 Self-Evolution，不新增 Candidate 业务规则。

## 真实迁移内容

- `RuntimePaths` 新增 `evolution_db_path`，从平台原生 `NAUMI_STATE_HOME`/state home 一次解析；
- `RuntimeResources` 与 override 新增 `evolution_candidate_store`；
- `build_runtime_resources()` 在全部 override 预验证后构造默认 Store；
- Engine 使用 `resources.evolution_candidate_store` 创建 FeedbackIntakeService 与
  EvolutionReviewService，不再调用 Store 构造器或路径 resolver；
- FeedbackIntakeService 改为必须显式注入 Store，移除 `store or default` 的隐式 Service Locator；
- `/feedback` 缺少已装配 Service 时失败关闭并给中文诊断，不在命令层另建旁路 Store。

`persistence/store_catalog.py` 仍可调用只读路径 resolver 来描述备份/完整性目录；它不创建 Store，
不违反 Resource ownership。

## 不变量

1. Evolution DB 始终位于工作区之外的用户状态目录，bypass 不改变位置。
2. 构造路径/资源包不创建目录或 SQLite；首次真实 Candidate 写入才建立 DB。
3. 显式 override 保持对象 identity，Feedback 与 Review 必须收到同一个实例。
4. 无效 Evolution override 在 Harness 默认 Store 构造前失败，不能留下半对象图。
5. 生产源码只有 Composition Root 可调用 `EvolutionCandidateStore(...)`。
6. 缺失 Service 不得静默回退到全局默认数据库。

## 验收证据

- RuntimePaths 验证 `evolution.db` 的平台状态路径与 absolute/canonical 约束；
- RuntimeResources 验证默认路径、显式 identity、完整 bundle 与预验证顺序；
- AST gate 验证 Engine 不导入/构造 Store，生产构造点唯一位于 Composition Root；
- FeedbackIntakeService 拒绝缺失/错误 Store；已有 feedback 脱敏、幂等、聚合测试保持通过；
- 真实 Composition Engine 完成流式文件工具与 Harness receipt 后，通过注入 Feedback service 写入
  Candidate，并确认同一 `resources.evolution_candidate_store` 创建真实 SQLite；
- 文档治理与相关小模块通过，不运行全量测试。

## 自我审视与后续

这是实际生产消费者迁移：Feedback 和 Review 都依赖注入 Store，命令 fallback 也已删除，不是只把
已有默认构造包一层。

当前仍未迁移 ChatRun、Task/Workbench、Background/Scheduler、Goal/Pursuit、Browser 与 Worktree；
其中带连接、Runner 或 daemon 的资源必须与 ARC-01.4d 生命周期合同一起推进。ARC-01.4c 还需迁移
FeedbackIntakeService、EvolutionReviewService 本身的 Service 构造权；因此 ARC-01.4 仍为 partial。
