# UI-10.6a Workbench Proposal 决策交互

## 交付目标

在已有 Reviews 页中统一展示 waiting Approval 与 open Proposal，并为 Proposal 提供可审计的
`approve`、`reject`、`cancel` 交互。New UI 与 Textual TUI 复用
`WorkbenchService.govern_proposal()` 和 HAR-09.5b1 状态机，不创建第二套 Review 页面或治理逻辑。

本切片中的“批准”只把 Proposal 从 `open` 转为 `approved`。它不执行代码、不创建实验、不转换 Issue、
不写 Git，也不授予 EVO-02 实验资格。

## 数据与协议

- `dashboard_snapshot()` 的 Reviews 计数等于 waiting Approval 加 open Proposal；初始选择同时携带
  `review_id` 与 `review_kind`，避免两类对象 ID 相同时选错。
- 客户端事件：`workbench/proposal/action`；服务端结果：
  `workbench/proposal/action_result`；协议能力：`workbench_proposal_actions`。
- action 仅允许 `approve|reject`；reject 原因必填，最多 2000 字符；Proposal ID、session ID、布尔确认
  均在 Python 协议边界校验。
- Node 只接受公开、定长、强类型 Proposal 字段，丢弃未知私有字段。结果状态固定为
  `needs_confirmation|completed|blocked|conflict|not_found|error`。
- 当前会话检查、权限判断、状态转换、CAS 冲突与权威快照刷新全部发生在 Python Bridge/Service。

## 权限与审计

- `workbench_govern_proposal` 是高风险、必须确认的显式权限规则；normal/permissive/moderate/strict
  模式在写入前要求用户确认。
- bypass 是全权限：批准可直接发送，拒绝在填写必需原因后直接发送，不再出现二次确认。
- bypass 只跳过确认，不跳过 Service 状态机、CAS、cooldown、事件审计或权威快照刷新。
- 拒绝沿用 `proposal-governance-v1` 的审计与冷却；并发终态冲突返回权威状态，不由前端覆盖。
- Bridge 的运行时异常使用固定中文提示；可操作的契约/状态冲突保留有界中文原因。

## New UI 交互

- Reviews 列表保持 Approval 在前、open Proposal 在后，并使用不同文字标签与语义色。
- Proposal 详情展示风险、来源 Candidate/revision、类型、影响范围、Agent/Task、目标文件和验证计划。
- `a` 批准，`x` 拒绝；拒绝先输入原因。normal 模式显示确认页，`y`/Enter 确认，`n`/Esc 取消。
- bypass 批准直接提交；bypass 拒绝在原因 Enter 后直接提交。
- loading 阶段吞掉重复按键；成功后使用返回的权威 Snapshot 替换列表；取消不写入任何状态。
- 页面与草稿均为进程内瞬态状态，新进程不恢复上一次未提交的决策。

## TUI fallback

- Textual Reviews 使用同一 Snapshot 合并 Approval/Proposal；Approval 继续懒加载真实 Git/验证证据。
- Proposal 使用 `a`/`x` 和瞬态决策弹窗。拒绝原因为空时不能提交。
- normal 模式的弹窗提交构成明确确认；bypass 批准不弹窗，拒绝只保留必需原因输入。
- TUI 直接调用同一 PermissionChecker 与 Workbench Service；成功后重新读取权威 Snapshot。

## 验收证据

- Python：协议合法/非法输入、normal 确认、bypass 无二次确认、跨会话、Service Snapshot 选择与权限规则。
- Node：严格 Proposal 投影、事件 registry、决策输入/取消/确认/bypass、80/120/200 列渲染。
- Textual：Approval 证据兼容、Proposal 预览、拒绝原因校验、normal 拒绝、bypass 直接批准。
- HAR-09.5b1：approve/reject 幂等、CAS 冲突、reject cooldown 保持既有定向测试。
- 不运行全量测试；本切片只运行上述相关小模块。

## 明确未包含

- waiting Approval 的 approve/reject 动作；
- Proposal `defer` 日期/原因表单与 `merge` 目标选择器；
- approved Proposal 到 EVO-02 Experiment Contract 或 Workbench Issue 的显式转换；
- HAR-09.6 before/after outcome tracking；
- UI-10.5 Timeline 与 revisioned domain patch。

下一切片应优先实现 approved Proposal 到 EVO-02 隔离实验契约的显式转换前置；在该契约完成前，
不要把批准扩展成代码执行。`defer/merge` 交互可以作为独立的 UI-10.6b 跟进。
