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
