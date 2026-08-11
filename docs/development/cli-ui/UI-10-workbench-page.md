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
- UI-10.8 Release tab：结构化显示 Stable Population finalization current authority 与撤权原因。

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

### UI-10.8 已实现：Stable Population Finalization Authority

- `EVO-05.5f5x3j` 的 current View 通过只读 typed reader 进入现有 Workbench snapshot；读取不会创建 Receipt、
  启动任务或授予任何执行权。
- projection 严格区分 pending、completed、revoked，并固定 config/data finalization 与 promotion authority
  为 false；source/校验失败只暴露固定 unavailable 码。
- New UI 与 Textual TUI 都增加 `4 Release` 页签，使用同一后端事实和中文撤权原因；前端只校验、着色和
  有界渲染，不自行推导 Snapshot/member authority。
- 真实 x3j→Workbench→Bridge→Node 与 Textual 交互、小/中/宽视口均已定向验证。完整契约见
  `UI-10-8-stable-population-finalization-authority.md`。

### UI-10.5a 已实现：权威 Timeline 快照

- New UI 与 Textual TUI 复用 `WorkbenchService.dashboard_snapshot()` 中最近 50 条持久审计事实，
  不解析聊天文本、不新增日志 Store，也不授予任何执行权。
- 两端都严格限制事件数量、字段长度、severity 和 payload 深度；数组只显示数量、嵌套对象不递归展开，
  身份字段控制字符或越界结构失败关闭，payload 文本在显示边界安全归一化。
- Timeline 按权限、Git、Harness、Agent、工具、工作台分类；严重级别覆盖类别色。New UI 在
  80/120/200 列有界渲染，TUI fallback 使用同义符号和文本。
- `5`/`l` 打开 Timeline，方向键/Home/End/PgUp/PgDn 导航；刷新按稳定 event id 保留选择。
- 完整契约、验收证据和后续增量边界见 `UI-10-5a-authoritative-timeline-snapshot.md`。

### UI-10.5b 已实现：Timeline 增量恢复

- SQLite 为每个 session 持久化独立 Timeline stream/cursor；快照元数据与最近事件从同一读事务取得。
- Bridge 支持显式订阅、取消、连续增量、断线有界 replay 与 gap 完整快照；生命周期绑定当前 session。
- New UI 与 Textual TUI 都验证会话、stream 和连续 cursor；重复事件幂等，跳号或换流失败关闭并恢复。
- Timeline cursor 不推进 Dashboard revision；完整契约与验收见 `UI-10-5b-timeline-cursor-authority.md`。

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

### UI-10.6c 已实现：approved Proposal → Experiment Contract

- Reviews 继续展示 approved Evolution Proposal，并使用独立 `c` 动作签发或重开不可执行 Contract；
  open Proposal 的 `a/x` 行为保持不变。
- New UI/Bridge/TUI/Agent Tool 复用 EVO-02.1b issuer。Store 按 workspace/session/Proposal 单飞，重复和
  不同 seed 并发调用都收敛到同一 durable Authority。
- normal 模式一次确认，bypass 直接执行；任何模式都不跳过 Candidate provenance、approved state、scope、
  budget、Git baseline 和 Store 校验。
- 成功回执显示 Contract/Authority identity 并固定 `execution_ready=false`、`promotion_ready=false`；前端
  严格拒绝绝对/越界/重复文件路径、非法 digest、越界预算和 readiness 提权。
- 完整用户状态机见 `UI-10-6c-experiment-contract-action.md`，后端契约见
  `../harness/HAR-09-5c-explicit-experiment-contract-issuance.md`。

### UI-10.6d/HAR-09.6a-6c2a 已实现：Proposal Outcome 与分阶段结果证据

- Reviews 同时保留 `approved` 治理事实和 `rolled_back` 实施终态，不把两种状态压成一个枚举。
- New UI/TUI 显示 Outcome、Rollback Receipt、Contract、breach 与 authority；黄色表示 rollback，绿色表示
  authority 有效，红色表示证据失效或来源不可用。
- 终态 Outcome 或不可用 source 会移除 `c` 动作；`EvolutionExperimentContractIssuer` 同时在服务端阻断，
  因此前端事件、Agent Tool 或 Slash 都不能绕过。
- HAR-09.6b 让 Reviews 显示 proposal-bound Before/After Evidence ID 和 lane count，口径固定为“实施前
  RED baseline → 实施后 GREEN candidate”；前端拒绝把它冒充为回滚后评测。
- HAR-09.6c1 显示新的 Post-Rollback Verification ID、baseline slot/version 和 fresh boot + launch identity；
  同时明确“行为级 Eval 尚未记录”，避免把可启动性扩大为业务恢复。
- HAR-09.6c2a 已通过三端共享 Slash/Tool 暴露首个 fresh installed-runtime H5c 单平台 lane；该阶段仍显示
  “行为级 Eval 尚未记录”。后续 6c2b1/6c2b2 已分别完成跨平台矩阵聚合与 typed Reviews projection。
- HAR-09.6c2a1 已通过 `/evolution outcome-behavior-coverage` 在三端展示完整 lane 覆盖、missing/stale 与
  目标主机调度数；typed Reviews coverage panel 仍是后续切片，当前不得把 Contract 显示为 matrix completion。
- HAR-09.6c2a2a 已通过 `/evolution outcome-place-behavior` 在三端显示 exact Worker incarnation 与 release
  target；回执必须保留“健康/容量未验证、无执行权”，不得显示为 queued 或 running。
- HAR-09.6c2a2b 已通过 `/evolution outcome-resolve-behavior` 在三端显示受信 channel/target、source
  commit/tree 与 Build Attestation；回执必须保留“未下载、未安装、未下发、无执行权”。
- HAR-09.6c2a3b 已通过 `/evolution outcome-dispatch-behavior` 在三端显示 exact Worker、suite/repetitions、
  执行预算、fresh Health sequence 与原子 capacity reservation；回执必须保留“未 claim、未传输、无执行权”。
- HAR-09.6c2a3c 已通过 `/evolution outcome-claim-behavior` 在三端共享 prepare/submit/renew：challenge 显示
  canonical payload 与 signable digest，receipt 显示 exact Identity 和 lease epoch；始终保留“未传输、无执行权”。
- HAR-09.6c2a3d 已通过 `/evolution outcome-deliver-behavior` 在三端共享 prepare/submit/inspect：offer 显示
  envelope、archive/manifest 与 ACK digest，receipt 明确“已交付但未安装、无执行权”；专用 typed delivery
  panel 和自动 remote push 仍是后续切片。
- HAR-09.6c2a3e 已通过 `/evolution outcome-authorize-behavior` 在三端共享 prepare/submit/inspect：Start challenge
  显示 exact Attempt、suite/budget、deadline 与 signable digest，authorization 显示 Worker signature、Run Grant、
  Runtime lease 和 expiry；仍明确“没有真实 start/result 证据”，不投影 Matrix completed。
- HAR-09.6c2b1/6c2b2 已完成完整行为矩阵及 typed Reviews 投影：New UI/TUI 显示 Matrix ID、总体 verdict、
  platform、lane kind 与 local/remote evidence source；Matrix stale 或 binding/聚合篡改时失败关闭。长期指标、
  promoted Outcome 和 policy learning 仍未完成。完整边界见
  `../harness/HAR-09-6a-proposal-outcome-projection.md` 与
  `../harness/HAR-09-6b-before-after-outcome-evidence.md`、
  `../harness/HAR-09-6c1-post-rollback-runtime-verification.md`、
  `../harness/HAR-09-6c2a-post-rollback-behavioral-lane.md`。
  Coverage 前置见 `../harness/HAR-09-6c2a1-post-rollback-behavioral-coverage.md`，Matrix UI 见
  `../harness/HAR-09-6c2b2-post-rollback-behavioral-matrix-ui.md`。

### UI-10.6b1 已实现：Proposal defer

- open Proposal 增加 `d` 延后；New UI 与 Textual TUI 都收集必填原因和 1/7/30 天有界预设。
- 绝对 `defer_until` 由 Python authority clock 生成，再交给既有 HAR-09.5b1 状态机执行 CAS、cooldown
  和 `proposal.deferred` 审计；前端不自行计算治理状态。
- normal 模式保留一次确认；bypass 在参数齐全后直接执行且不显示二次确认，但不跳过状态机与审计。
- defer 详见 `UI-10-6b1-proposal-defer.md`；merge 由下述 UI-10.6b2 独立交付。

### UI-10.6b2 已实现：Proposal merge

- Python authority 为每个 open Proposal 投影最多 20 个同 Candidate、较新 revision、仍 open 的目标；
  New UI/TUI 不自行推导合法性。
- 两端都支持键盘选择目标；normal 保留一次明确确认，bypass 选择目标后直接提交且不追加二次确认。
- Bridge 将目标纳入权限参数，Service 写入前重新读取并重验 session/Candidate/revision/state，随后以 CAS
  把源标记为 merged、保留目标 open 并写 `proposal.merged` 审计。
- 详见 `UI-10-6b2-proposal-merge.md`。merge 不执行代码、不签发实验或 promotion 权限。

### UI-10.6e 已实现：waiting Approval approve/reject

- New UI 与 Textual TUI 在 Reviews 中以 `a/x` 决策 waiting Approval；拒绝原因必填。
- 普通模式一次确认；bypass 参数齐全后直接提交，不增加高风险二次确认。
- Store 只允许 `waiting → approved/rejected`，并把终态与 `approval.resolved` 审计写入同一事务。
- 并发、重复或迟到决定返回 conflict 与最新 Snapshot，不覆盖先到终态。
- Approval 是人工控制面，Agent 不获得自批 Tool；批准不执行代码、不签发实验或发布权限。
- 完整契约与验收见 `UI-10-6e-waiting-approval-actions.md`。

### 当前边界

- UI-10 功能切片已完成；跨 Runtime 实例通知、Timeline 长期历史翻页与外部审计 archive 不属于本模块，
  分别由 HAR-10、ARC-06 和后续审计模块承接。
