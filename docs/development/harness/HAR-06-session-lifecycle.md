# HAR-06 Session 生命周期与派生记录治理

## 目标

Session 的 archive/delete/retention 与 Harness 派生记录保持一致，既不留下不可解释孤儿，
也不误删用户要求保留的审计证据。

## 子模块

- HAR-06.1 Lifecycle policy：`retain|archive|delete|legal_hold` 四种策略。
  - **已实现**：封闭策略/操作者枚举、失败关闭转换决策、legal hold 审计门、Session 状态适配器。
- HAR-06.2 Reconciliation hook：Session 操作成功后触发幂等 Harness 清理。
- HAR-06.3 Tombstone：删除失败时记录安全 tombstone，后台重试。
- HAR-06.4 Artifact GC：只删除没有其他引用且通过 workspace/session 校验的 artifact。
- HAR-06.5 Retention worker：按天数、空间上限和最近访问执行有界清理。
- HAR-06.6 User surfaces：删除预览显示将影响的 run/evidence/artifact 数量。
  - **HAR-06.6a 已实现**：CLI/New UI/TUI 与只读 Agent Tool 共用 workspace-scoped
    删除影响预览；Artifact 只展示引用数，不冒充可删除文件数。

## 当前进度（2026-07-17）

HAR-06 当前为 `partial (6.1, 6.6a)`。已落地的前置能力包括：

- `harness.retention` 定义无副作用的生命周期决策；`legal_hold` 阻断后台转换，唯一自动
  状态变化为 `archive -> delete`，且只有 `delete` 策略允许自动清理。
- 现有 Session Store 的 `active/archived` 通过失败关闭的单一适配器映射到
  `retain/archive`，为 HAR-06.2 提供稳定边界。
- `HarnessStore.preview_session_delete()` 使用 SQL 聚合统计精确 workspace + session 的
  Run、Criterion、Check、Evidence、Replay Baseline 与 Artifact 引用，不加载记录正文。
- 历史的 Harness 删除原语现已强制提供 workspace，消除了相同 session ID 跨工作区误删风险。
- `AgentEngine.preview_session_delete()` 以 Session 保存的 workspace 为权威作用域；仅对缺失旧元数据
  回退当前工作区。
- `/history delete-preview <ID>` 已同步 CLI/New UI/TUI；Agent 可用只读
  `session_history(action="delete_preview")` 获取相同结果。

尚未实现 HAR-06.2-06.5；因此当前 `/delete` 仍只处理 Session Store，不会把本预览中的 Harness
记录或 Artifact 自动删除。跨 Store delete saga 必须等 lifecycle policy、tombstone 与引用安全 GC
具备后再接入，详见 `HAR-06-6a-session-delete-preview-design.md`。

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
