# 跨项目依赖与接口边界

## 1. 权威归属

| 事实/能力 | 唯一权威 | 消费者 |
| --- | --- | --- |
| Agent 执行与工具结果 | Python Runtime | UI、Harness、Eval |
| 权限决定 | PermissionChecker | UI、Harness Evidence、Audit |
| 任务/Agent 状态 | TaskStore/SubAgentManager | UI、Harness、Pursuit |
| 完成真实性 | Harness Completion Gate | UI、Goal/Pursuit、自进化 |
| UI 本地折叠/焦点 | Frontend UI state | New UI only |
| 长期目标 | GoalStore/PursuitStore | Runtime、UI、Harness |
| 自进化是否提升 | Eval + Reflection Decision | Promotion Manager |

不得由消费者复制或反推权威状态。例如 UI 不能根据颜色猜工具成功，Pursuit 不能根据模型
一句“完成了”绕过 Harness，Self-Evolve 不能根据单次测试自行提升。

## 2. 关键依赖

- UI 消费 ARC-03 版本化协议，不能直接 import Runtime 内部对象。
- HAR-08 Eval 消费 Harness Store/Evidence；EVO-03 再消费 Eval，不另建评分管线。
- ARC-04 daemon 复用 PermissionChecker 和 Tool metadata；不能提供旁路执行 API。
- HAR-10 长周期编排与 Pursuit 合并控制语义，避免两个无限循环竞争同一任务。
- CC 源码迁入只替换或增强 Frontend/Extension 层，不取得 Python 权限和任务权威。

## 3. 兼容策略

- 协议：major 不兼容、minor 向后兼容、patch 仅修正文档/实现。
- Store：只做前向幂等迁移；降级必须在发布说明声明可否读取。
- UI：新 UI 默认；TUI fallback 至少支持提交、权限、取消、任务、回执、诊断。
- Tool：旧 schema 在一个稳定发布周期内保留 adapter 和弃用警告。

## 4. 集成顺序

1. ARC-01/03 先固定边界和协议。
2. HAR-05/08 提供回放与评测裁判。
3. UI-10..16 在稳定协议上完善产品表面。
4. ARC-02/04/06 服务化和并发化执行层。
5. EVO-01..06 逐门开启自进化能力。
6. ARC-07/08 完成发布、更新、SLO 与灾难恢复。

任何逆序实施都必须在 PR 中说明临时 adapter、移除条件和验证范围。
