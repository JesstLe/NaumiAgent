# HAR-06.2a Session 删除协调状态库设计

## 问题

Session Store 与 Harness Store 是两个独立 SQLite 数据库，无法用一个数据库事务原子提交。
直接“先删 Session，再删 Harness”会在进程退出时丢失 workspace 和待清理引用；反过来执行则可能
在 Session 删除失败时先破坏审计证据。

## 本切片

本切片实现持久化、幂等、单向的协调状态机，但暂不接管 `/delete`：

1. `prepared`：在任何删除前，把 request id、workspace、session、actor、Run 数量和 Artifact
   引用快照写入 Harness DB。
2. `session_committed`：Session Store 已确认删除后推进；未到此状态严禁删除 Harness 行。
3. `records_committed`：在一个 Harness SQLite 事务中删除精确 workspace/session 的 Run 及其
   级联子行，并原子推进状态。

状态只能前进或幂等重放，不能回退或跳级。相同 request id 重放必须返回同一记录；若作用域、
Session 或 actor 不同则视为幂等键冲突并失败关闭。

状态库提供按更新时间排序、带硬上限的未完成请求查询，使 HAR-06.2b 在进程重启后无需预先知道
request id 也能恢复。前向转换的 `updated_at` 不得早于当前记录；幂等重放保留原时间。

## Artifact 约束

准备阶段保存 Check `artifact_path` 与 Evidence `artifact://` URI 的类型化引用快照。当前模块不
删除任何文件；HAR-06.4 将对这些引用执行 workspace 路径校验、去重和引用计数。即使 Harness
Run 行已级联删除，后续 GC 仍有权威输入。

## 故障语义

- 崩溃在 `prepared`：Session/Harness 均未被协调器改动，可从记录恢复用户意图。
- 崩溃在 Session 提交后、状态更新前：HAR-06.2b 恢复器以 Session Store 是否仍存在为权威，
  再幂等推进。
- 崩溃在 Harness 删除事务中：SQLite 保证删除和状态推进同时提交或同时回滚。
- 重放 `records_committed`：不再次删除，返回已保存结果。
- 失败重试次数、错误码和后台调度属于 HAR-06.3 tombstone，不在本表伪造简化版本。

## 验收标准

- v2 Harness DB 可加法迁移到 v3，旧 Run/Replay 数据不丢失。
- 准备记录不加载 objective/evidence summary，不保存原始命令或模型输出。
- 未确认 Session 删除时无法清理 Harness。
- 相同 session-like ID 跨 workspace 不互相影响。
- Artifact 引用在 Run 行删除后仍可跨进程恢复。
- 真实 Session DB + Harness DB 完成准备、Session 删除、推进、Harness 清理与幂等重放。
