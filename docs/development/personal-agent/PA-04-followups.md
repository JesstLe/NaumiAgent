# PA-04：截止跟进与事件驱动建议

日期：2026-11-05
基线：`7e0a3063cf514198c8a24e753766ad147e4b78d8`
状态：模块已实现，本地协议 / HTTP / 生产组件浏览器闭环通过；完整 Personal Agent 目标保持进行中。

## 用户路径与职责

`用户启用规则 → 原截止 / 已提交的日历变化 → 持久建议 → 原通知中心 → 人工采纳 / 忽略 / 稍后提醒 → 原今日安排 / 原日历行动草稿 → 原对象读回`。

- 初始没有规则，也不初始化任何职业型 Agent。规则只绑定用户已创建的 Agent，主 Agent 的原域版本为 0。
- 入口位于“今日”的“安排 / 跟进”分段，不增加全局一级导航。浅色布局，只显示当前操作需要的信息。
- 截止来自原 Today metadata，和 Item 的执行时间分开。采纳只安排原事项，不创建 Item、Job 或 Run。
- 日历建议来自原 Google 同步提交后的变化记录，不靠定时请求模型猜测事件。
- 日历采纳创建真实原 `CalendarAction(status=draft, operation=insert)`；后续人工审批、外部写入和读回继续由原连接模块负责。
- 跟进扫描不调用模型、不请求 Google、不启动第二套执行器，不替用户审批行动。
- 跨工作区汇总保持只读；本模块规则与采纳只操作当前工作区。

## 原事件变化记录

`calendar_changes.py` 复用原 ConnectionStore，保留 schema/checksum v1。同步发布时，每次原事件变化与单调序号、不可变 change 记录在同一事务提交；分页暂存不产生变化。所有可用性分支统一经过 `_publish_calendar_event`。

- 显式启用规则时 enrollment；已有镜像只记 `baseline / enrollment_snapshot`，不伪称历史 provider 变化。
- 未来变化规则从 enrollment head 后开始；包含已有事件需要用户明确选择。
- 变化引用绑定 connection instance、auth epoch、exact grant、事件历史摘要和 occurrence identity。
- A → B → A 是两次真实提交变化；重复同一事件内容不重复造建议。
- provider 取消、完整快照遗漏、recurring master 取消、实例移除、恢复分别保留原原因。
- 取消或不可用状态只返回 sanitized status/available；建议不恢复旧标题、正文、时间或链接。
- freshness 取原成功同步时间。扫描只暂停过时来源，不自行强制同步。

## 原截止与时间规则

- Deadline trigger 可显式选择事项，或明确选择该 Agent 可读的全部截止事项；不会扫描成跨 Agent 私有资料。
- `trigger_at = 原截止 UTC - offset_minutes`；支持提前、到点、逾期的有符号偏移。
- date-only 截止沿用原 civil day 的 exclusive end；IANA 时区、fold/gap 与 TZif 摘要沿用原 Today 合同。
- 一轮只采用最新的合法阶段，不补发多个过时阶段。`latest` 受最大延迟窗口约束；`skip` 只允许 60 秒宽限。
- 人工已勾选、metadata 归档、原事项关闭或取消，都停止新的截止建议；不会伪造 Job 完成状态。
- 语义去重排除 metadata revision、priority、标题和扫描时刻；原事项语义、截止、阶段与 rule epoch 决定身份。
- 改规则名称或暂停不重置 epoch；改触发语义、时区或显式重绑 actor 配置才产生新 epoch。
- worker 在取得 durable fence 后重新核对最新时钟高水位，事务内禁止降低 highwater；系统时间回退时保持原进度。
- 每轮最多 5 条规则，每条最多 50 个候选；metadata 循环分页，日历 change 顺序推进，保留 partial 和 cursor。

## 持久状态与人工决定

FollowupStore 使用原 OwnerJournal 的 domain hashes、历史修订、CAS、租约与 fencing。独立 `rule / scan / proposal / decision / effect / read / cursor` 记录各自承担职责，proposal/read/cursor 只追加。原会话数据库旁的外部 pin 绑定目录实例；只读入口不会建库或重新绑定丢失的目录。

- `proposal` 是不可变来源事实，`decision` 独立记录 pending / snoozed / accepted / dismissed。
- 忽略只需本地 exact proposal 和 decision CAS；暂停规则或原来源失效后，用户仍能关闭不再可读的 opaque 提示。
- snooze 是持久 UTC 时刻；醒来增加 presentation epoch，只产生一次新通知，不重复创建目标。
- 采纳先保存原人工输入、固定目标 payload、原 channel、稳定 child request 和 effect，再调用原域。
- 同请求重复采纳查询原回执；并发相同请求只获得一个 effect 与一个原目标。
- UNKNOWN 保留原 child request/payload，先查原域回执再恢复；不换 ID 试一次，也不删除已提交回执。
- 原目标 CAS 冲突变为 blocked。只有原 child receipt 已证明 missing 时，新人工请求才可重新确认目标输入。
- readback 重新读取原 Today day/placement/metadata/history 或原 CalendarAction，核对 receipt 与 provenance；本地 materialized 字段本身不构成成功。

## 人工来源与模型资料

原 Today 与 CalendarAction 增加 server-private bridge，不接受 JSON 自报的 effect。固定 apply ContextVar 与 lease holder/fence 绑定这一次执行；原域提交 callback 同步检查 exact effect、payload 和当前 fence。旧执行者不借用新租约。

采纳产物引用历史人工决定，不要求当前规则仍启用。用户暂停规则后，已安排目标继续按原域状态显示；原资料撤权仍使原产物、派生产物和行动依据失效。

ArtifactStore 新资料使用 `authority_version=5`，保留 strict `followup_read_refs / followup_effect_refs` 与完整 ancestry。修订去掉显式来源时仍保留旧版本引用；移除跟进组件或回退旧 reader 都 fail closed。旧普通资料及 v2/v3/v4 来源仍按各自原合同读取。

模型工具只有 context / rules / list / get；规则变更和全部人工决定只由 Web / CLI / TUI 注入可信 channel。实际返回字节与私有产物 SHA 绑定，原 session 和 actor 参与验证，下次模型发送前重新授权。通知模型资料单独固定 presentation 引用；已解决的新读取只包含 opaque 状态，不携带旧来源正文。

## 共享入口

- Tool：`personal_followups`，只读模型操作。
- CLI / TUI：`/followups`，共用 `execute_operation()` 与严格字段验证。
- REST：`GET /personal/followups/context`、`POST /personal/followups/operations`，沿用原认证、速率限制和 no-store。
- Web：今日“跟进”、规则编辑、来源核对、忽略/稍后/采纳、原目标打开。
- 通知：原中心新增 `followup_proposal`，normal priority，沿用勿扰与合并；不新增绕过或提高注意力等级。

## 验证记录

事务目录：`.naumi/transactions/pa04-followups-20261105/`。

- 写入前封存 3147 个 tracked 原件及 SHA，基线探针 `FOLLOWUPS=ABSENT`，exit 1。
- Calendar feed 最终组：112 passed / 3 skipped；接口/旧通知入口组：305 passed。组之间不相加冒充无重叠总数。
- 前端最终集中终验：36 files / 534 tests passed，生产 tsc/build 和实际三生产组件 harness build 均 exit 0；包含原 action 生命周期、重复选择同一建议、初次读取失败后的恢复、操作期限及停止等待。保留早期 514 / 528 / 530 项运行记录与失败。
- 初次 effects 组保留 1 failed / 17 passed，失败是相同接受请求的并发 replay 检查顺序；已修正并进入完整复验。
- 后端 service 全文件 42 passed；effects 全文件分两个互斥分组覆盖 21 项，均 exit 0。其冻结前后 11 个关键源码摘要一致。
- 自审又修正模型 rules 旧快照绑定新 ref、effect origin 任意正文、并发首请求已受理而来源随后变化的回执恢复。最终 authority 全文件及两条原域 effect 集中组 26 passed / exit 0，冻结后源码摘要一致。
- 原 Today / CalendarAction / Notification / Artifact / Workspace / Routine 相邻组 375 passed / 2 skipped / 2 弃用 warnings。期间只有两个明确 root authority/dispatch 修正，前后摘要差异保留；这些新路径另经最终 26 项组验收。不把上述重叠组相加，不称全仓 pytest 已过。
- 真实 loopback Google-adapter HTTP、跟进生产组件浏览器、原 Today / CalendarAction 读回、日历 postcommit UNKNOWN、两次冷重启、勿扰/稍后/忽略、遗漏/恢复/取消及撤权清正文均已通过。最终清理与源码事务身份以本事务 VERIFICATION.txt 为准。
- 本地协议 / HTTP / 浏览器验收与真实 Google 账户、外部模型生成、安装发布、容量和多设备验收分别记录，不相互替代。

## 实际读回耗时与等待控制

真实 HTTP 暴露了深层来源校验超过旧通用 20 秒客户端期限的情况。客户端现在为跟进写入、原请求核对与原目标读回设置独立 180 秒期限，并保留明确的“停止等待”操作；停止等待保留原请求身份，不撤销或重放已受理的服务端效果。通用其他请求的 20 秒默认不变，工作区切换和断线保留取消等待语义。

这项修正保证可恢复的完整交互，不构成速度或容量优化声明。原目标读回中重复的 effect 资料及 provenance 授权仍需独立测量优化；后续优化必须保留原 actor/grant、exact bytes、末次来源授权和撤权栅栏。首次 UI 的一条服务端 503 原响应未捕获，后续同 proposal 24 次并发读取均 200；未将该条未知原因改写为已修复的后端根因。

静态读放大分析保存于本事务 `PERFORMANCE_REVIEW.md`，状态为 NOT_MEASURED；其中的调用次数是静态估算，不是实测性能。

原路径第二次冷重启后的 7 个串行、单次、无自动重试 HTTP 均为 200，另存
`http-fixture-01/cold-restart-02-http-latency.json`：audit 30.936 秒，日历建议 16.493 秒、原人工请求
16.926 秒、原 Action GET 0.063 秒；Today 建议 24.152 秒、原人工请求 29.044 秒、原 Day GET
9.192 秒。这组数据证明现有 20 秒等待不足，也明确说明深层读回开销仍未优化；不是优化前后对照或生产 SLA。

第二次冷重启真实重开原 native envelope，并核得两 effect 冻结输入、revision、apply lease 与完整 lease rows
不变；原 child request 指纹、原 provenance、唯一一个 placement 与一个 INSERT 草稿均通过读回。
approval / execution 仍为空，原 Item 与完整 scheduler facts 摘要一致，没有新 Run / Job 或 provider 写入。

## 真实浏览器与原来源闭环

- 原规则由真实 UI 创建和编辑。初次 provider B 只变 summary，而默认规则只选 start/end/status，因此正确不产生建议；通过同规则 ID 的人工 CAS 编辑显式纳入名称/描述后，下一真实 provider A 产生序号 4 的有效建议。未绕过字段过滤或重建规则。
- 首次截止采纳发生响应等待超时，沿原 request `92f7f7ad89a74c4ba9c513114fe1e08a` 重新核对并读回同一 Today placement，没有第二次 accept。
- 唯一日历 accept 创建原 INSERT 草稿；`calendar_after_commit` 确实在原目标提交后注入异常。UNKNOWN 时原 audit 已证明原 receipt / 目标存在，随后只对同一 effect reconcile 并读回。
- 第二次冷重启前后保持同一 placement / draft / effect / child request 与原 provenance；后续通过真实源变化验证 snooze → wake epoch → dismiss。
- provider 410 → 完整快照遗漏产生序号 6 的不可用建议，正文 sanitized；恢复产生独立序号 8 和新 proposal，随后取消仍只显示泛化状态，不恢复旧标题/正文。
- 撤销原 Calendar 连接后，已编辑的私密预填表单被清除；真实模型来源材料、独立派生资料及 next-send 重新授权均阻断。最终两条规则由原 UI 归档，不删除历史目标。
- 首次失败、脚本等待预算错误及每个续跑 checkpoint 均保留；最终成功以 `followup-ui-targets17.json`、`followup-ui-tail18.json` 为证，不把首轮改写为 PASS。

## 全仓测试与基线对照

实际执行 `python -m pytest tests/ -x -q --tb=short`，首个
`tests/e2e/test_agent_control_center.py::test_real_manager_bridge_node_stop_and_textual_parity`
在第 134 行 `target_started.wait()` 的 1 秒期限处失败。pytest 报告 `1 failed, 4 warnings in 323.86s`，
并包含 executor 线程等待 300 秒的告警；测试进程随后仍未退出，已仅终止核实过身份的自有 pytest。

冻结基线副本与当前源码分别做 90 秒有界对照，两者都出现首项 F 并在退出阶段挂起。
随后只加入原 pytest report hook 的 45 秒对照，已在 teardown 前分别捕获原 call 报告：
两份失败 repr 完全相同，均为第 134 行的原 1 秒启动等待 TimeoutError。保留完整命令、
原输出、边界终止与 report JSONL，不将被终止的运行称为通过，也不修改该独立模块来拼出绿色。

因此本模块集中测试与真实本地闭环分别记录，**全仓 pytest 未通过**；总目标和发布验收继续保持未完成。
对照材料见本事务 `full-repo-x-01.json`、`full-repo-call-report-summary.json` 与两份 `first-node-*-reports.jsonl`。

## 尚未纳入本模块的完整验收

真实 Google 账户写回读回保留原独立待验收状态；其他个人服务、系统级推送、电脑执行、语音、跨设备及整体发布仍按原 24 项目标推进。本模块不以建议条数当执行成功率，不扩大既有账户授权，也不把这一次模块提交当完整 Personal Agent 交付。
