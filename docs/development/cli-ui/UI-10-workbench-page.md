# UI-10 `/workbench` 命令页

## 目标

把 Mac/Windows Workbench 后端的任务、worktree、review、timeline 和变更摘要带入终端，形成
只读优先、动作受权限层控制的统一命令页。

## 子模块

- UI-10.1 Bridge snapshot：revision、task/worktree/review counts、active selection。
- UI-10.2 Overview：目标、状态、owner、分支、变更、检查、风险。
- UI-10.3 Worktrees tab：路径、branch、dirty、lease、占用 Agent。
- UI-10.4 Reviews tab：待审 diff、检查、审批、阻塞原因。
- UI-10.5 Timeline tab：工具、Agent、权限、Git、Harness 事件统一排序。
- UI-10.6 Actions：open/detail/review/approve/reject/cancel，全部走 Python 权限和 service。
- UI-10.7 TUI fallback：同数据的简化页面，不复制 Store 查询。

## 验收标准

- `/workbench` 首帧只读，不创建 worktree 或启动 Agent。
- revision 断序时请求完整快照；重复 snapshot 幂等。
- approve/reject 有对象 id、预览、二次确认和审计；bypass 仍记录决策。
- 选中/折叠不持久化到新 Session；resume 只恢复权威工作状态。
- 80/120/200 列、0/1/100 个 worktree/review 均可操作。
- 真实 Workbench Store/Bridge/Node/TUI 四段链路通过。

## 非目标

不在终端复制 Swift Workbench 全部视觉，不让前端直接执行 Git。

## 实现进展（2026-07-17）

### UI-10.1 已实现：Bridge Revisioned Snapshot

- 现有 `WorkbenchService.dashboard_snapshot()` 是唯一 producer；每个 Service/session 代际生成
  `stream_id`，每个 session 按规范内容指纹维护单调 `revision`。相同内容重复查询保持 revision，
  内容变化递增；后端实例重启或有界状态淘汰通过新 stream id 与旧 revision 空间隔离。
- schema v1 完整快照增加 `generated_at`、`full`、mission/task/worktree/review/failure counts 和
  active mission/task/worktree/review selection。worktree 从 issue/lease 权威字段去重，待审从 waiting
  approval 得出，前端不重新猜测。
- Python 与共享 JSON contract 新增 `workbench/request`。Bridge 只读取当前会话，失败返回固定脱敏
  中文错误；`/workbench` 走专用请求，不进入对话、不调用模型、不创建任务、worktree 或 Agent。
- New UI 只接受当前会话快照；相同 stream 的重复/旧 revision 幂等忽略，新 stream 的 full snapshot
  可替换旧状态。revisioned event 只有严格连续时才先追加；UI-10.1 尚未定义 domain patch，因此连续
  事件也会请求完整快照且不提前推进 snapshot revision。断序、缺失基线或 stream 变化不会追加，
  刷新完成前不会重复请求或污染时间线。
- 用户显式请求成功后显示紧凑同步回执（任务/worktree/待审数量）；完整 Overview 视觉页仍属于
  UI-10.2，不在本切片伪装完成。
- 真实 SQLite Task/Workbench Store 经新的 Service、Bridge JSONL、Node normalizer/reducer 验证：
  重复快照 revision 不变、任务状态变化 revision +1、counts/selection 与后端一致。

### 尚未完成

- UI-10.2：Overview 页面及 80/120/200 列布局。
- UI-10.3：Worktrees tab。
- UI-10.4：Reviews tab。
- UI-10.5：Timeline tab 与 revisioned 增量事件生产。
- UI-10.6：受权限控制的动作。
- UI-10.7：TUI fallback parity。
