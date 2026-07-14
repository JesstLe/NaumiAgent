# HAR-06 Session 生命周期与派生记录治理

## 目标

Session 的 archive/delete/retention 与 Harness 派生记录保持一致，既不留下不可解释孤儿，
也不误删用户要求保留的审计证据。

## 子模块

- HAR-06.1 Lifecycle policy：`retain|archive|delete|legal_hold` 四种策略。
- HAR-06.2 Reconciliation hook：Session 操作成功后触发幂等 Harness 清理。
- HAR-06.3 Tombstone：删除失败时记录安全 tombstone，后台重试。
- HAR-06.4 Artifact GC：只删除没有其他引用且通过 workspace/session 校验的 artifact。
- HAR-06.5 Retention worker：按天数、空间上限和最近访问执行有界清理。
- HAR-06.6 User surfaces：删除预览显示将影响的 run/evidence/artifact 数量。

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
