# HAR-06.6a Session 删除影响预览设计

## 用户问题

当前 `/delete` 和 `/history delete` 会直接删除 Session，但 Harness Run、Check、Evidence、
Replay Baseline 和 artifact 引用位于另一物理 Store。现有 `delete_session_records(session_id)`
甚至没有 workspace 条件；若不同工作区出现相同 session-like ID，直接接入会误删另一工作区
的审计记录。

## 本轮范围

本轮实现只读、workspace-scoped 的删除影响预览，并收紧现有 Harness 删除原语：

1. `HarnessStore` 用 SQL 聚合精确统计目标 workspace + session 的 Run、Criterion、Check、
   Evidence、Replay Baseline、Check Artifact 引用和 Evidence Artifact URI 引用。
2. 统计不加载 Run objective、evidence summary 或 artifact 内容；10k Run 仍保持常量级应用
   内存。
3. `AgentEngine.preview_session_delete()` 先读取权威 Session，再使用 Session 保存的 workspace
   （旧 Session 缺失时才回退当前 workspace）查询 Harness。
4. `/history delete-preview <session-id>` 在 CLI/New UI/TUI 共用同一模型和 renderer；Agent
   通过只读 `session_history(action="delete_preview")` 获得相同结果。
5. 现有 `delete_session_records` 强制要求 workspace，杜绝后续误用无作用域删除。

## 预览语义

预览中的 artifact 数量是“引用数”，不是“可安全删除文件数”。同一文件可能被多个 Run 引用，
URI 和相对路径也可能表示同一物理对象；HAR-06.4 在引用计数和路径校验完成前，不得把该数字
描述成将删除的文件数。

预览本身始终无副作用。HAR-06.2c 完成后，`/delete` 已通过 lifecycle policy、持久协调状态机
与 tombstone 安全删除 Session 和精确作用域的 Harness 数据库行；预览仍用于操作前风险判断。
Artifact 数量依旧只是引用快照，HAR-06.4 完成前不会删除物理文件。

## 错误与边界

- Session 不存在：返回稳定的“不存在”，不查询任意 workspace。
- Harness DB/表不存在：计数为 0，不创建数据库。
- Harness DB 损坏或忙超时：预览失败并提示状态库不可读，不降级成 0。
- 相同 session ID 跨 workspace：只统计目标 Session 保存的 workspace。
- 空 workspace metadata：只对旧 Session 回退当前 Engine workspace，并在结果中展示该作用域。
- 输入必须是明确 Session ID；不接受历史列表数字编号。

## 验收证据

- Store 测试：多 workspace 同 ID、全部派生表计数、artifact 引用、空库、损坏库。
- Engine 真实场景：临时 Session DB + Harness DB 建立真实记录并生成预览。
- Surface 测试：共享 renderer、Agent Tool、CLI command 和 TUI command 都走 Engine 方法。
- 删除原语测试：只删除精确 workspace/session，另一 workspace 行数不变。

## 后续

HAR-06.4 artifact GC 应消费协调记录中的不可变引用快照，重新校验 workspace、Session 和共享引用
后再删除物理文件。HAR-06.5 再把恢复与 retention 扩展为有界周期 worker；两者都不得改变本预览
的只读语义。
