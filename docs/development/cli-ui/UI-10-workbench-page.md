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

## 实现进展（2026-07-18）

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

### UI-10.2 已实现：全屏 Overview

- `/workbench` 现在保存对话 timeline 锚点并进入独立全屏 Overview；`r` 只读刷新，`Esc` 恢复原
  scroll/follow-tail。页面路由和 Workbench 快照不进入 UI session snapshot，新进程默认回到 conversation。
- 显式 resume 若发生在 Workbench 页面，会丢弃旧会话快照、清零旧 timeline 锚点并请求新会话权威
  快照，避免把旧会话选择或滚动位置带入新会话。
- Overview 展示 active mission 的目标/状态、active task 的说明/owner、issue/lease 的 branch/worktree/PR、
  最近 validation 的命令/状态/退出码/耗时，以及 risk/failure/waiting approval。缺失字段明确显示“尚未绑定”
  或“尚未记录”，不从模型文案猜测。
- `>=120` 列使用目标/任务与变更/验证/风险双栏；窄屏纵向降级。高/严重风险与验证失败为红色，
  待审/中风险为黄色，进行中为青色，完成/通过为绿色；关闭 ANSI 后仍有完整中文标签。
- loading、empty、ready、error 四态均有可行动提示；100 个 worktree/review 只展示权威 counts 与当前
  对象，不展开巨大列表。组件和完整 `renderScreen` 在 80/120/200 列均验证无溢出。
- 终端进程真实执行 `/workbench`→渲染→`r`→`Esc`，确认没有 `submit` 聊天事件。SQLite Store→
  Service→Bridge→Node reducer→Overview renderer 的 80/120/200 列链路同时通过。

### UI-10.7 已实现：TUI fallback parity

- Textual TUI 输入 `/workbench` 会打开全屏 Overview，直接调用当前 Engine 的
  `WorkbenchService.dashboard_snapshot(current_session)`；没有第二套 Store 查询或状态推导。
- 页面展示与新 UI 相同的权威目标、任务、owner、branch/worktree/PR、最近验证、风险、失败和待审；
  UI-10.6a 之后，Reviews 同源展示 waiting Approval 与 open Proposal，并支持受权限控制的 Proposal
  approve/reject/cancel；其他页面仍保持只读。
- schema/version/stream/revision/full/session 任一不匹配都会拒绝快照；刷新失败保留上一次成功快照，
  首次失败提供 `/doctor` 下一步，不泄露底层异常。
- Store 文本先做控制字符清理、长度限制和 Markdown 转义；80/120/200 列均保留核心状态，空任务有
  明确创建/刷新提示。

### UI-10.3 已实现：权威 Worktrees tab

- `WorkbenchService` 直接从 Engine 注入的 `WorktreeManager` 读取 Git 权威状态，并与当前 Task、active
  lease、占用 Agent 合并；New UI、HTTP API 和 Textual TUI 不再各自猜测或重复查询载体状态。
- 快照明确区分 `ready`、`unavailable`，提供稳定诊断码、真实总数和 200 条传输上限。Git 状态读取失败时
  Overview 其他任务/审批/验证数据仍可使用，底层异常内容不会进入用户界面。
- New UI 提供 Overview/Worktrees 页签，支持 `Tab`/`Shift+Tab`、`1`/`2` 切换，方向键、Home/End、
  PageUp/PageDown 精确导航。列表只渲染当前视窗，0/1/100 条均不会撑爆终端。
- 宽屏使用列表/详情双栏，窄屏纵向降级；状态、dirty、ahead、lease、Agent、任务、路径、分支和可安全
  删除均有文本标签及语义色。选择只存在当前 UI 进程；刷新优先保留同名项，项消失时落到原索引附近。
- Textual fallback 使用同一份快照增加 Overview/Worktrees 页签与上下选择；不提供直接 Git 动作，不绕过
  后续 UI-10.6 的权限、预览和审计设计。
- 真实临时 Git 仓库创建 managed worktree 并写入未提交文件，经 SQLite Store、Service、JSONL Bridge、
  Node reducer 与 80/120/200 列 renderer 验证；重复只读刷新保持 revision 不变。

### UI-10.4 已实现：只读 Reviews tab

- Dashboard Snapshot 继续作为待审列表唯一事实来源；Node 协议现在保留经过有界规范化的 waiting
  approvals，不再丢弃后端已经返回的 review 数据。列表支持 `3`、Tab/Shift+Tab、方向键、Home/End、
  PageUp/PageDown，0/1/100 项均只渲染当前视窗。
- 选中 Review 后通过 `workbench/review/request` 懒加载单项证据；Bridge 只允许当前会话，并调用既有
  `WorkbenchService.get_review_evidence()`。响应严格绑定 review id，缺失项返回稳定 unavailable 状态，
  内部异常只返回脱敏诊断码。
- Review 详情展示审批说明、发起者、worktree 状态、验证次数和失败数、变更文件以及有界 unified diff。
  新增/删除/修改和 hunk 使用绿/红/黄/青语义色；无 ANSI 时仍保留 `+/-/~` 与中文状态。
- 页面把“worktree 缺失”“未运行验证”“验证失败”“证据就绪”明确区分，但不替用户做审批判断；
  approve/reject 仍属于 UI-10.6，未在本切片提前开放。
- Textual TUI 增加同源 Reviews 页签，直接调用同一个 Service，不复制 Store/Git 查询。Review 选择和
  已加载详情都是进程内瞬态状态，新进程或新会话不恢复旧选择。
- ReviewEvidenceCollector 的 changed files 限制为 200 条，并拒绝解析到配置 worktree 根目录之外的
  名称；diff 继续限制为 30 个文件、每个 4000 字符，避免异常仓库撑爆终端协议。
- Python 协议/Bridge/真实 Git evidence、Node contract/reducer/80/120/200 renderer、Textual 交互和
  终端进程均有定向验证。完整设计和边界见 `UI-10-4a-reviews-tab.md`。

### UI-10.6a 已实现：Proposal approve/reject/cancel

- Reviews 复用一个列表展示 waiting Approval 与 open Proposal，权威 counts 和 selection 增加对象类型，
  不新建第二套 Review 页面。
- New UI 通过 typed action/result 协议调用 Python 权限层和既有 Workbench Service；normal 模式确认，
  bypass 无二次确认，所有模式保留状态机、CAS、cooldown 与审计。
- Proposal 详情显示来源、风险、影响、目标文件和验证计划，并持续提示批准只进入下一 policy gate，
  不执行代码、不授予实验资格。
- Textual fallback 使用同一 Service/PermissionChecker，支持 normal 拒绝原因与确认、bypass 直接批准。
- 完整契约、验收证据和未完成边界见 `UI-10-6a-proposal-actions.md`。

### 尚未完成

- UI-10.5：Timeline tab 与 revisioned 增量事件生产。
- UI-10.6b：Proposal defer/merge 表单与 waiting Approval 动作。
