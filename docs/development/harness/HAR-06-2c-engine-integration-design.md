# HAR-06.2c Engine 与用户界面协调接入设计

## 用户问题

Session 与 Harness 位于不同持久化边界。只删除 Session 会留下孤儿审计记录；把 Harness 故障
当作普通失败又会让已经消失的 Session 继续保有当前会话和工作区权限。进程在两阶段之间退出时，
下一次启动还必须从持久状态继续，而不是依赖原调用栈。

## 本轮范围

本切片把 HAR-06.2b/6.3b 的协调器接入实际运行路径；当时尚未扩展 Artifact 文件删除：

1. `AgentEngine` 持有唯一 `HarnessStore` 与 `SessionReconciliationCoordinator`，所有删除入口共用
   相同 lifecycle policy、确定性 request id、状态机与 tombstone。
2. `delete_session_detailed()` 返回封闭结果：完整完成、安全重试、重试耗尽、不存在、策略阻止。
   原 `delete_session()` 保留布尔兼容，但只有完整完成才返回 `True`。
3. CLI/New UI、TUI、Agent Session Tool 与 API 使用详细结果，不把部分完成压扁成模糊布尔值。
4. CLI 交互启动、单任务启动、TUI mount 与 API lifespan 各执行一次有界恢复；单次最多领取固定
   数量 tombstone，并使用租约避免并发进程重复执行。
5. 本切片不启动常驻定时 worker，当时不删除 Artifact 文件，也不改变删除预览的只读语义。

## 权威状态与取消语义

Session Store 决定运行时会话是否仍存在。协调结果进入安全重试时，如果 Session 已经提交删除，
Engine 必须立即清除当前 Session、临时消息、工具授权与工作区授权；Harness 行可由后续恢复继续清理。
这避免“持久 Session 已不存在，但旧运行态仍可操作”的权限幽灵。

调用方取消不等于允许留下不确定运行态。协调任务先在屏蔽取消的完成路径中落下成功状态或安全
tombstone；随后 Engine 对 Session 存在性做权威检查。即使调用方连续取消，检查也会完成，最后仍
向调用方传播取消。删除互斥继续复用现有 Session transition lock，避免 active session 切换竞态。

## 用户界面结果映射

| 协调结果 | CLI / TUI / Tool | API |
|---|---|---|
| `completed` | 已删除并完成 Session/Harness/Artifact 协调 | `204 No Content` |
| `retry_scheduled` | 已进入持久安全重试，展示 request id | `202 Accepted` |
| `retry_exhausted` | 重试耗尽，需要人工检查 | `503 Service Unavailable` |
| `not_found` | 会话不存在 | `404 Not Found` |
| `policy_blocked` | 生命周期策略阻止删除 | `409 Conflict` |

TUI 删除当前会话后立即清空聊天面板；只要 Session 已提交删除，即使 Harness 正在重试也刷新历史
列表。启动恢复只在确实领取到记录时更新状态栏，空扫描不制造噪声。

## 验收证据

- 真实临时 Session DB + Harness DB：成功路径同时删除 Session 与关联数据库行，并撤销运行时权限。
- 注入 Harness 清理故障：Session 删除保持权威，创建持久 tombstone，下一次恢复完成剩余阶段。
- 取消回归：删除前、删除中、提交后以及连续取消均不遗留权限幽灵或未记录 crash gap。
- Surface：CLI、TUI、Tool 和 API 对部分结果给出不同且稳定的用户反馈。
- 启动：CLI/TUI/API 只执行有界恢复，空队列无提示或副作用。

## 后续状态

- HAR-06.4 已在后续切片完成，详见 `HAR-06-4-artifact-gc-design.md`。
- HAR-06.5：周期 retention worker、批次预算、取消、空间上限与观测指标。
- 仍不能宣称“所有 Artifact 已清理”：HAR-06.4 会有意保留共享、风险、非普通文件和不受管路径。
