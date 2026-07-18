# ARC-01.4b2e Goal 与 Pursuit Resource Pair

## 目标与边界

Goal 是工作区长期目标的权威状态，Pursuit 是目标分解、行动、证据与等待任务的运行记录。两者通过
`Goal.pursuit_run_id` 关联，但不共享 schema，也不能由 UI 根据自然语言输出反推关系。本切片把
`GoalStore` 与 `PursuitStore` 作为一个资源对交给 Composition Root，消除 Engine 内的默认构造。

本切片不实现 HAR-10 的租约、心跳、checkpoint 或集群恢复，也不把 Pursuit 改造成第二个 Harness
循环。它只完成后续类型化 Goal/Pursuit UI 与长周期可靠编排共同需要的资源所有权前置。

## 路径、初始化与资源合同

- `RuntimePaths.goal_storage_dir` 固定为 `runtime_data_dir/goals`；
- `RuntimePaths.pursuit_storage_dir` 固定为 `runtime_data_dir/pursuit`；
- 两个路径均为 absolute/canonical，且不得逃逸 `runtime_data_dir`；
- `RuntimeResources` 必须同时持有具体 `GoalStore` 与 `PursuitStore`；
- override 在任一默认资源构造前校验，显式实例保持 identity；
- Engine、Goal tools 与 Pursuit loop 只消费注入实例，不重建默认 Store。

两个 Store 都改为线程安全的 lazy initialization：构造只保存规范路径，不创建目录或数据库；首次
真实读写在实例锁内幂等建表。Goal 的唯一未完成目标索引继续承担跨线程竞争裁决，Pursuit 的 run、
evidence 与 wait 继续按现有事务边界持久化。

## 稳定关联与恢复

关联方向保持单一：Goal 保存 Pursuit `run_id`，PursuitRun 保存自己的稳定 ID 和目标文本。恢复时先读
Goal 的 `pursuit_run_id`，再从 PursuitStore 读取运行事实；不存在的 run 必须被上层呈现为缺失/待恢复，
不能凭目标文本匹配另一条记录。

真实组合根场景验证：创建 Goal、保存 PursuitRun、绑定 ID，再分别重开两个 Store，关联 ID、目标和
运行状态均可恢复。它证明持久化引用稳定，但不等同于 HAR-10 的动作幂等或进程级 checkpoint。

## 架构门与验收证据

- Engine 禁止导入或构造 `GoalStore` / `PursuitStore`；
- 两个默认 Store 在产品源码中只能由 Composition Root 构造；
- Store 构造后目录不存在，首次操作才建库；
- Goal 并发创建仍只有一个请求获得未完成目标，其余收到可纠正错误；
- Runtime Composition 覆盖默认路径、override identity、不完整 bundle 和路径逃逸；
- 真实 streaming Engine 验证资源 identity、稳定 ID 关联、重开恢复与原有 receipt 链路；
- 仅运行 Goal/Pursuit、Runtime Composition、架构门和单个真实集成测试，不运行全量测试。

## 自我审视与后续

本实现没有建立跨数据库外键，因此 Goal 关联与 Pursuit 写入仍是两个事务；进程若在二者之间崩溃，
上层需要 HAR-10 reconcile 识别孤立 run 或缺失引用。当前也没有 lease、epoch、heartbeat、checkpoint、
interaction queue 和 destructive action 幂等键，不能宣称长周期恢复已经完成。

下一步不继续机械迁移 Background/Scheduler Store。先交付 UI-18.1 类型化 Goal/Pursuit snapshot，直接
复用本切片的权威资源与稳定 ID；之后再依据 HAR-10/ARC-02 的依赖选择最小可靠性前置。
