# CLI/TUI/New UI 后续模块册

## 产品边界

- `naumi` 默认启动 `frontend/terminal-ui` 新 UI。
- Textual TUI 是可靠 fallback，必须保留核心任务能力。
- 旧 prompt_toolkit CLI 代码保留但 deprecated，不再接受独占新功能。
- Python Bridge 拥有运行/权限/任务事实；Node UI 只拥有焦点、折叠、滚动等本地状态。

## 当前已完成地基

UIMessage、JSONL protocol、tool/activity card、semantic rendering、completion receipt、runtime
inspector、agent control center、tasks、permissions、history、heartbeat、working animation、跨平台
启动、类型化 Goal/Pursuit 只读页、New UI/Textual TUI 共用的 durable interaction authority，以及
UI-15.1a 的 New UI 有界 stream delta 合并与控制事件绘制屏障已存在。HAR-10.3b4 已补齐 TUI 运行中输入的
durable queue、连续 claim 和 `/send-now` parity；HAR-10.3b5 已增加双端 `/cancel-queued` 和明确的未派发取消
状态。UI-16.6a 已让 working indicator 重新显示受限、脱敏的运行性能阶段，同时避免等待态展示过时指标。
UI-16.3a 已让 `/q`、空闲 Ctrl+C 与 macOS/Linux/Windows 可捕获信号进入同一安全退出状态机，等待
Python Bridge 完成 draining、持久化与资源清理后返回同请求 ID；超时、重复信号和 fatal path 均有有界兜底。
UI-15.5a 已将 New UI 触摸板/滚轮方向 burst 从直接丢弃改为首步立即、48ms 匀速、最多一行待输出且
反向可立即打断的控制器，慢滑可逐行定位，快速滑动不会形成长惯性队列。
UI-15.5b 又将 Textual TUI 的全局 pointer sensitivity 从默认每事件 2 行收敛为 1 行，所有
VerticalScroll 页面沿用同一逐行策略。
UI-10.8 已把 EVO-05.5f5x3j 的 Stable Population finalization current View 投影到 Workbench；New UI 与
Textual TUI 共享 `4 Release` 页，严格区分 pending/completed/revoked、显示撤权原因，并固定不授予
config/data finalization 或 promotion 权限。
UI-10.5a 已把同一 Workbench 快照中的持久审计事实投影为 New UI/Textual TUI Timeline；事件严格有界、
按权限/Git/Harness/Agent/工具分类并保留语义色，revisioned 增量 push 留给 UI-10.5b。
UI-10.6b1 已让 New UI/Textual TUI 在同一 Workbench Reviews 页延后 open Proposal：原因必填，
时长限定 1/7/30 天并由 Python authority clock 生成精确 cooldown；normal 一次确认，bypass 参数齐全后
直接执行且不出现二次确认。UI-10.6b2 又补齐 merge：后端投影同 Candidate 的较新 open revision，
两端键盘选择，Service 最终重验并以 CAS 写入 source merged/target open 和审计；bypass 不增加二次确认。
UI-10.6e 已补齐 waiting Approval 人工决策：New UI/TUI 共用 waiting-only CAS 与同事务审计，拒绝原因
必填，普通模式一次确认，bypass 参数齐全后直接执行；并发或重复决定返回 conflict 和最新权威快照，
Agent 不获得自批人工 gate 的 Tool。
UI-13.1c 与 ARC-01.4c1-4c3 已让 New UI/TUI Doctor 都展示各自 terminal lifecycle 的实时 retention 状态；
UI-13.1d/1e 又让两端从同一只读 Worker authority 看见 capacity 与 durable queue backlog；缺少
Composition 注入时明确标记不可观测，而不是伪造调度健康。UI-17.1 已为两端发布 14 项严格 capability manifest；
UI-13.2a 已让 Provider 本地配置与显式 live probe 失败产生低基数稳定诊断码，并由 New UI/TUI/CLI 共用；
UI-13.3a 已让 New UI、Textual TUI、CLI 与 Agent Tool 共享最多 1 请求、8 输出 token、默认 15 秒超时、
无自动重试的显式 Provider 探测，并提供精确取消与 typed 终态；打开或刷新 Doctor 仍保持零 Provider 流量；
UI-13.4a 已把当前 DebugTrace 的最近 2 MiB 投影为正文折叠、可筛选、带确定性 Snapshot 的 typed 索引，
New UI/TUI/CLI/Agent Tool 共用相同 authority，旧 Bridge 未协商能力时不会误发请求；
UI-13.5a 已让 New UI/TUI/CLI/Agent Tool 先预览固定 3 文件的脱敏诊断 ZIP，再以精确 Snapshot 摘要把
同一 Bundle 原子写入平台 Naumi 状态目录；包不含聊天、reasoning、raw trace、环境变量全集、凭据或源码；
UI-17.3c 已进一步让 New UI 在旧 Bridge 不声明 `doctor_export` 时保留 Health 页面、显示兼容提示并阻止
preview/write 发送，Python Bridge 也会在 Plan/写盘之前拒绝未协商请求；
UI-17.2a-17.2f 已用共享 fixture 锁定 Bridge、TUI 与 Node reducer 的 runtime-health 八字段语义、权限脱敏、
bypass/session grant 四选择、模型主动询问和 canonical answer、submit/tool/receipt/cancel 基本运行生命周期，以及
Evaluation Lane RED/GREEN、资源证据与强制非最终边界，以及 token 合并、相关错误断流、发送 retry identity 和
New UI/Textual 生产渲染路径的固定视口 ANSI/text capture；
TUI 现在也能用 Ctrl+C 取消当前运行，空闲时不会误退出。
HAR-07.5c1 已让 New UI/TUI/CLI 共享 `/copy receipt [receipt-id|latest]`，从当前 session 的持久
Completion/Harness authority 生成同一有界、脱敏文本并保存到 `.naumi/exports`；完成卡显示精确入口，
剪贴板不可用时仍保留文件。
UI-14.1a 已建立 New UI/TUI 共用的严格 command index，现有补全可以展示参数 syntax、来源、category 和权限风险；
UI-14.2a 已进一步交付两端 `Ctrl+P` 命令 QuickOpen，支持别名、说明、类别、风险和 fuzzy 搜索，选择只填入 composer；
UI-14.2b 已增加本次启动内的隐私安全最近命令排序，只记录规范命令名且新启动重置；
UI-14.2c 已复用 UI-11 类型化任务快照增加两端任务 provider，`Tab` 切换并只填入只读详情命令；
UI-14.2d 已复用 ARC-03.2b2 工作区会话快照增加两端会话 provider，选择只填入 `/load <id>`；
UI-14.2e 已增加 Engine-owned、可取消、workspace 隔离、最多 100k 文件的后台索引，两端文件 provider
选择只填入安全 `/read` 模板；
UI-14.2f 已复用 Agent Control one-shot snapshot 增加两端 Agent provider，选择只填入
`/agents agent <name>` 并在显式提交后定位详情；
ARC-04.5a 已继续扩展同一 Agent Control execution descriptor，让 New UI/TUI 详情显示模型调用前签发的
Worker 请求摘要、终态结果摘要、精确工具范围与合同降级码，不在前端重算合同；
ARC-04.5c 又以 additive 字段同步 durable Agent job ID、state、claim epoch 与稳定降级码；两端只消费
Agent Control authority，不直接读取 Agent Job SQLite，也不展示 owner、expiry 或敏感 payload；
HAR-10.7c/ARC-06.2c 继续把共享 active/waiting 上限、reclaimable 与 recovery-required 计数加入
summary，并以 `waiting_capacity` 呈现尚未调用模型的执行：New UI 使用绿/黄/红状态色，Textual
复用同一权威状态；
ARC-04.5d2c 已在 Agent Control schema v3 增加只读 `results` section：New UI/TUI 都能查看
当前 session 已认证投递的结果、usage 与摘要，公开摘录统一脱敏并限制为 2000 字符；两端不直接读取
AgentJob SQLite，也没有伪造 read/ack/retry 操作；
HAR-10.7d 已将 Agent Control 升级到 schema v4，增加严格有界、逐条认证的只读 `recovery_catalog`；
两端“恢复”标签用红/黄/蓝区分需裁决、可接管/待投递和 live 状态，只公开稳定 ID、epoch、expiry、
摘要与 reason code，不公开 owner 或 task/context/response；HAR-10.7e 又为当前会话
`recovery_required` Job 增加协商后的 `u` 动作，两端共用 Manager/Store exact fence，无二次确认但会
复验 request/session/receipt/epoch/expiry，动作回执在刷新后仍可见，且不自动重放模型；
UI-14.2g 已增加 surface-aware 权威页面索引，两端页面 provider 只填入精确导航命令；
跨启动历史、typed argument form 与 Vim/input mode 尚未实现。
两端不再依赖各自的临时排队状态。
后续模块不得绕开这些路径重建新状态层。

HAR-10.8b 已让 Goal/Pursuit 的最近机械边界裁判进入共享 typed projection：New UI 与 TUI/CLI
fallback 显示相同 code、status、短 decision id 和原因；前端只校验并渲染，不重算终态。
HAR-10.8c 将恢复裁判升级为 schema 2；New UI 同时严格读取 schema 1/2，TUI/CLI 继续消费同一
Python projection。durable pending interaction 显示为 waiting，缺失 authority 才显示 blocked。
HAR-10.8d 又为恢复动作建立持久、幂等的 attempt 账本；当前状态命令可以查看最近请求，
UI-18.5b1 已让 New UI 通过 typed ToolExecution 发起受控 resume，并让 TUI fallback 显示同源动作、
共享命令和最近 attempt；HAR-10.8e 又提供 fenced `/pursue reconcile <attempt-id>`，只按更高
RunLease epoch 和准入后的 checkpoint/机械裁判收口。两端均不得自行猜测准入或完成。
HAR-10.8f 已继续交付自动 terminal outbox 核心、worker、死信审查/重入队/放弃动作；takeover/cleanup
与恢复历史 cursor 尚未实现。

UI-18.2a 已把 Goal 页面从历史目标平铺升级为稳定目录选择：New UI 使用 `←/→` 或 `[/]` 发送明确
`selected_goal_id`，TUI/Agent Tool 使用 `/goal detail <goal-id>` / `goal_status(goal_id=...)` 消费同一
Python snapshot。所选详情显示当前快照内全部有界 wait/evidence，不再由 Node 二次裁成最后 5 条；
Goal 历史与完整 evidence 时间线 cursor 仍属于 UI-18.2 后续切片。

UI-18.3a 已把首个可逆 Goal 写动作接入共享 ToolExecution：New UI 对所选 active/paused Goal 使用
`m` 暂停/恢复，结果以 GoalStore 重读为准；Textual TUI 与 Agent Tool 继续使用同源 `/goal pause`、
`/goal resume`，旧 Bridge 明确降级到命令通道。create/block/complete/cancel 仍属于 UI-18.3 后续切片。

## 未来顺序

UI-10/11/12/13 可按顺序独立交付；UI-14/15/16 可并行；UI-18 按 Goal/Pursuit 后端依赖分段推进；
UI-17 是统一发布门；17.1 manifest 已完成，17.2a runtime-health、17.2b permission/interaction 与
17.2c terminal run lifecycle、17.2d Evaluation Lane Receipt、17.2e stream recovery 与 17.2f terminal capture 已完成；17.3a 已验证
New UI 对旧 Bridge 的 Evaluation Lane typed→Slash 降级，17.3b 已建立发布合同驱动的通用
event-capability registry，17.3c 已完成 Doctor Export 的失败关闭降级。仍需断连 uncertain/权限恢复 golden、
未知关键事件分类和 17.3 其余治理、17.4-17.6。
