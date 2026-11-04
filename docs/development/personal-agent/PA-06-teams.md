# PA-06：团队日常对话与持久团队回合

日期：2026-11-04

状态：**partial**。本切片已接通团队会话、持久分工、成员执行、汇总与写回，
当前真实模型、真实 HTTP 浏览器与新进程冷重启验收通过。
本轮精确命令、原始哈希、整合失败对照与源码副本回滚结果统一记录在 `.naumi/transactions/pa06-teams-20261104/VERIFICATION.txt`；回滚结果以实际执行记录为准，不以脚本存在代替通过；
不以既有 peer 切片证据替代团队验收，也不将审查前专项结果冒充修复后的最终回归。

本记录接续 [PA-06 委派与专员消息](PA-06.md)。全部 24 项产品范围保持不变；
完整产物版本、私有文件 ACL、语义记忆、个人事项和外部服务等后续模块仍保留。

## 1. 用户可操作的实际行为

- 初始只有主助理。本切片不预置团队或角色；团队、名称、职责和成员均来自用户已创建的目录。
- 团队具有独立日常对话。用户可以直接打开团队，也可以让主助理向指定团队提交任务。
- 每次消息接纳为稳定 `pt_...` 团队回合；团队对话保留原用户消息、实际任务卡和最终回应。
- 同一团队会话的回合按原次序推进。新消息可以排队，后台汇总不覆盖新用户消息或其他会话。
- 已明确配置 `personal_team` 的团队协调器可以查询本次成员目录并提交真实 DAG，
  不通过换 prompt 模拟多个成员发言。
- 旧空工具团队仍可普通对话，不被静默授予分工能力。
  只有对话时，真实协调器回答直接交付，不凭空启动成员或汇总阶段。
- 成员执行记录使用独立会话和实际 Run；日常成员聊天不被覆盖，执行历史保持只读。
- 关闭页面或取消轮询不等于停止团队。停止操作针对原团队任务记录明确取消意图。

## 2. 唯一执行权威与三阶段设计

```text
团队日常会话
  └─ 不可变 TeamTurn：原请求、团队/成员版本、授权、来源与预算快照
       ├─ planning：协调器 Job / Session / Run
       │    └─ personal_team.propose 校验并持久提交成员 DAG，规划任务结束
       ├─ members：已有 PersonalCollaborationService 的成员 DAG Jobs / Runs
       └─ summary：新的协调器 Job / Session / Run
            └─ 校验真实成员终态，汇总后 append-once 写回原团队会话
```

`TeamTurnStore` 保存不可变输入、阶段请求/协作引用、计划提交、取消意图和交付事实。
它没有第二套 Job `status/result/lease` 生命周期。界面状态由既有 `AgentJobStore`、
`ChatRun` 完成回执和交付记录投影。

规划协调器提交计划后结束自己的 Job、释放容量，成员阶段随后入场；成员真实完成后才启动汇总。
协调器不会占住唯一容量槽并同步等待另外几个排队的成员 Job，因此
`max_parallel_agents=1` 仍可推进三个阶段。成员 DAG 的串行、并行、依赖、取消与租约恢复
继续复用已有协作服务，不另开不计容量的执行通道。

阶段请求使用团队回合派生的稳定 request ID。阶段创建前的完整请求先记录，
创建协作后、绑定引用前退出时重放同一请求，不产生另一份计划或成员执行。
只有规划 Job 和实际 Run 的完成证据成立，已提交的计划才会解锁成员阶段。

### 未核验终态

- `AgentJob` 已完成不代表 `ChatRun` 的完成回执自动成立。
- 缺少原 Run/receipt 时保留 UNKNOWN；`receipt.outcome=partial` 等非 completed 回执会阻断后续阶段。
- 不因模型说“完成”、已写入计划事件或短 UI 摘要而改成成功。
- 失败、取消、版本失效和 UNKNOWN 不自动重新派发。用户可打开原阶段核对实际证据。
- `delivery_pending` 与 `completed` 分开；最终完成表示真实阶段结束且结果已写入原团队对话，
  不表示用户已读或外部平台已送达。

## 3. 团队、嵌套成员与工具授权

- 顶层团队及每个嵌套团队均保存 ID、职责版本与确切成员关系。
- 展开后的授权对象是去重的叶子专员，而不是把嵌套团队当作普通成员步骤执行。
  最多 128 个团队、128 位叶子专员；嵌套深度有明确上限，不截断超限成员。
- 调用边界重查所有祖先团队的版本、成员关系与启用状态，以及已授权专员的版本和工具范围。
  内层团队移除成员时，即使顶层版本不变，原任务也会停止旧版本执行。
- 真实目录中属于团队的成员必须具有祖先版本快照，不接受伪造 roster 将它省略成“未授权叶子”。
- `member_grants` 省略表示所有叶子使用真实注册的只读工具及已配置的 `personal_message`；
  明确 `[]` 表示不授权任何成员；显式 `tool_names=[]` 表示该成员仅对话。
- 新回合与执行边界使用相同启用规则：所有祖先团队必须 active，只有本次实际授权的叶子必须 active。
  显式 `[]` 或 subset 排除的 paused/archived 叶子不阻塞提交；省略 grant 默认全选，
  因而仍会被停用叶子阻断。缺失成员、循环关系或把实际团队省略成未授权叶子仍然阻断。
- 写入、shell 等工具需要明确授予，接收方实际工具为其档案与本次 grant 的交集。
  schema 和真实执行入口同时限制，bypass 不绕过团队/成员范围。
- 协调器自己的工具来自团队档案，不继承成员工具并集；`personal_team` 仅适用于团队协调器。
- 专员私有聊天不进入团队规划或其他成员会话。规划可以使用本团队自己的既有回合对话；
  成员只接收明确任务、共享资料及绑定摘要的上游结果。

这里核验的是身份、工具和显式上下文边界，不将其冒充按专员隔离的完整私有文件 ACL。
文件工具仍受既有工作区/目录权限约束；产物版本与资料授权撤销继续由 PA-08 完成。

## 4. 请求身份、资料快照和预算

### 原请求不可变

- 接纳请求绑定来源会话、团队 ID/version、request ID、正文、上下文、附件编号和成员 grant。
- 来源必须属于当前工作区；主助理或同一团队日常会话可以发起，执行记录不作为新的来源日常会话。
- 附件从真实来源会话的资料记录解析，核对路径/工作区；接纳时将实际读取内容与摘要
  一起保存到不可变 `client_payload.source_context`。
- 请求和其资料快照在 TeamTurn 创建事务中共同落盘。后续重放返回原快照，
  不因附件文件后来改变而悄悄改变已受理任务的资料。
- 同一 request ID 改正文、版本、来源或授权会冲突。网络响应不确定时保留完整原载荷，
  查询或原样重试；一次列表缺行或错误文字不被当成“未受理”。
- 文本资料有读取与总大小上限；保存的是本次实际读取内容摘要，不冒充原文件全部字节的校验结果。

### 剩余额度，不是每阶段重新发满额

团队回合保存权限模式、每次执行轮数、费用和输入/输出 token 限额快照。
每阶段预算从原限额扣除已花费部分；成员共有 `n` 个步骤时按 `n+1` 份分配可用额度，
为后续汇总保留一份，汇总再取实际剩余额度。微美元与 token 向下分配，不因取整增发。
零额度保持为零，`None` 保持未设限。

用量按唯一根 Job 汇总。成员 Job 已包含 inline peer 消耗，团队不会再次把 peer Run
叠加进同一 Job 的总额；输入/输出统计保留实际 Run 用量。额度不足不会用新阶段重置额度。
根 Job 及各独立/peer Run 的分项必须能对账：缺失 Run 用量或输入、输出之和与根 Job 总 token 不一致时，
`token_breakdown_complete=false`，`input_tokens/output_tokens=null`，不将缺失用量写成零。
任一输入/输出 token 限额有限时，团队回合以 `unknown`、`error_code=usage_unknown` 保留不确定性，
不继续派发下一阶段。若两个 token 维度均未设限，则可继续按真实根 Job 费用执行费用限额，
但分项仍为 null，不伪造完整计数。

## 5. 不覆盖聊天的原子写回

`Session.personal_team_id` 是异步团队日常会话的不可变标记，
与 `personal_agent_id` 一致，且不与执行会话的 `personal_execution_id` 混用。

`append_personal_messages_once()` 在真实 SQLite 事务中读取最新历史，
以稳定 delivery ID、完整载荷摘要和消息 ID 去重，并同事务保存消息及用量。
重复交付不重复追加或计费；删除/归档的会话不被写回复活。

普通陈旧 `SessionStore.save()` 会保留已有团队会话的最新消息和用量，
不把后台交付或并发新用户消息覆盖成旧快照。标题等元数据更新不扩大消息写权限。

原用户消息和结果消息的 `metadata` 保存 `team_turn_id/team_id/team_version/source_session_id/request_id`，
结果另绑定确切 `result_sha256`。前端从真实消息接口刷新，不将后台结果直接拼进当前任意会话。

## 6. 入口与界面

### Tool 与 API

主助理通过 `personal_agents` 的 `team_send/team_turns/team_turn/cancel_team_turn`
操作调用同一个团队 service。内部协调器通过 `personal_team` 查询成员和提出计划；
工具不接受伪造 owner、epoch、执行会话或其他团队的成员范围。

- `POST /personal/team-turns`
- `GET /personal/team-turns?team_session_id=...`
- `GET /personal/team-turns/{turn_id}`
- `POST /personal/team-turns/{turn_id}/cancel`

接纳只表示原请求已落盘，不提前返回业务完成。各阶段问题和权限继续复用原协作接口，
绑定真实成员 session/call/question，而不是团队日常会话冒充成员身份。

### CLI / TUI

```text
/assistants open <已有团队ID>
/assistants team-json '<包含团队ID、当前版本、request_id、content的JSON>'
/assistants team-send-json '<同一JSON>'
/assistants team-turns [团队会话ID]
/assistants team-turn <团队任务ID>
/assistants cancel-team-turn <团队任务ID>
```

CLI/TUI 继续经过 `Engine.execute_tool(personal_agents, agent_name="cli")`，
显示受理与真实终态、成员历史版本和 Job/Run，以及完整多行结果，不绕过授权直调 service。
团队普通输入的 `admitted` 只表示已交给持久后台执行，不应显示失败或业务完成；
受理文本及稳定 `pt_...` 编号必须可见。TUI 队列的 `completed/background_task_admitted`
只表示本条输入已交接，TeamTurn 仍按实际后台状态推进，不冒充业务执行完成。
该分支与既有 pi TUI 的集中专项为 6 passed；最终两份团队集成/TUI模块共 36 passed。

### Web

沿用“我的助理”目录及原团队对话输入。任务卡位于对应原消息下，
阶段详情仅使用当前任务提供的阶段清单，不把全工作区协作历史当作团队历史。
查看执行、停止与需要决策的入口复用真实执行身份；不同成员相同 call ID 的权限仍分别匹配。

待核对 POST、草稿与轮询按 API、团队、原会话隔离；重挂载保留原请求身份。
后台刷新只更新当前会话消息，不清草稿或附件，不把旧会话的响应写入新会话。
停用团队、缺失成员与循环关系保留全局错误；停用叶子仅在仍被本次 grant 选中时阻止提交。
用户取消该成员勾选后可继续，UI 不静默提升版本、恢复停用成员或扩大授权。
完整 UI 细节见 [团队对话界面记录](../pa06-team-conversation-ui.md)。

## 7. 当前本轮证据

事务目录：`.naumi/transactions/pa06-teams-20261104/`。
以下为本地模块、受控集成、真实模型、浏览器及冷重启的不同层级，
不宣称整仓库全绿、部署或完整产品完成。

| 层级 | 证据 | 当前字面结果 |
| --- | --- | --- |
| 修改前身份/服务/API | `baseline.txt` | 65 passed in 19.60s |
| TeamTurn 持久存储 | `foundation-team-store-scope-tests.txt` | 54 passed in 0.93s |
| 会话基础设施聚合 | `foundation-tests.txt` | 141 passed in 10.15s |
| 请求/roster/DAG/预算与范围修复 | `scope-c-modified-backend.txt` | 166 passed in 8.95s |
| CLI/TUI 与旧命令回归 | `team-commands-tests.txt` | 181 passed in 6.48s |
| TUI 受理分支与 pi TUI | `team-tui-final-focused.txt` | 6 passed, 3 warnings in 10.30s |
| Engine 三阶段初验（最终审查前） | `team-integration-final.txt` | 26 passed in 111.36s |
| 前端最终全量（含范围修复） | `scope-c-modified-frontend.txt` | 25 files / 213 passed |
| TypeScript/Vite（含范围修复） | `scope-c-modified-build.txt` | built in 767ms |
| 真实 HTTP 浏览器 | `browser-result.json` / `browser-log.txt` | PASS；mocks=false；page_errors=[] |
| 新进程持久读回 | `cold-before.txt` / `cold-after.txt` | 2 turns / 7 root Jobs / 7 Run sessions / 4 messages；0 duplicates / POST / model calls |

上表 Engine 集成使用受控模型流、真实 SQLite/Engine/Job/Run；UI fixture 证明前端合同，
不替代真实模型/浏览器业务执行。各日志中的失败发现与修复记录保留，不通过删除断言制造完成。

### 已观察的真实模型验收

当前 `live-result.json` 为 **PASS**，`mocks=false`、`model_live=true`：

- 真实主助理查询现有目录并委托已有团队，不创建新角色或临时 Agent。
- 真实团队协调器规划，两个成员依次实际读取 `budget.csv`，独立计算并复核 `1200 − 375 = 825` 元。
- 汇总阶段根据成员确切结果输出金额、单位、来源与复核状态，不冒充协调器本人重新读取文件。
- planning 1 Job、members 2 Jobs、summary 1 Job，共 **4 个不同 Job、4 个不同 Run**；
  `max_parallel_agents=1`，没有等待槽位死锁。
- 原团队会话只追加一次用户消息与一次最终结果；结果摘要绑定一致，会话用量与回合用量一致。
- 主会话私有 sentinel 未进入任何执行会话。原请求重放保持原回合与原 Job 集合，无新派发。

### 已观察的真实 HTTP 浏览器验收

`browser-result.json` 为 **PASS**，`mocks=false`、`real_model=true`，浏览器命令退出 0：

- 打开原团队日常会话与已完成的四 Job 核算结果。
- 从真实 UI 提交新的团队任务，真实协调器提出结构化问题，用户在浏览器选择“通过”。
- 成员请求 `bash_run` 权限，浏览器操作绑定该成员自己的实际 session/call，而非团队日常会话。
- 成员实际执行 `echo TEAM_ACK`，stdout 为 `TEAM_ACK`、退出 0；新的真实模型汇总 Job 引用该结果。
- 结果仅写回一次，后台完成不清除用户已输入的新草稿。
- 1440 / 768 / 390 宽度均完成检查，`page_errors=[]`。四张真实页面截图保存在同一事务目录。

字面输出：

```text
TEAM_BROWSER=PASS; MOCKS=0; INTAKE=UI; QUESTION=REAL_COORDINATOR; PERMISSION=EXACT_MEMBER_SESSION; SHELL=TEAM_ACK_EXIT0; SUMMARY=REAL_MODEL; DELIVERY=ONCE; DRAFT=PRESERVED; WIDTHS=1440,768,390; PAGE_ERRORS=0
```

### 已观察的新进程冷重启

`cold_restart.py` 仅 GET，不创建或重放团队任务、不调用模型。先保存 `cold-before.json`，
确认两条原任务均已完成，再停止原 PID 28668；新的服务器进程为 PID 62768（启动器 PID 64588）。
新进程读回与原进程对象完全一致：**2 个团队回合、7 个根 Job、7 个 Run 会话、4 条原团队消息**，
保留原请求/Job/Run/消息 ID、回应、摘要与用量，重复消息 0、POST 0、模型调用 0。
该验收证明已完成任务持久读回，不宣称模拟了所有进行中崩溃点。

- `cold-before.json` 与 `cold-after.json` 的 canonical payload SHA-256 均为
  `d1a83e5e58db6ebf07952c795aabb646c85eb10f9a23faa5e7a1ea696b4e3cdb`，
  按 `json.dumps(value, ensure_ascii=False, sort_keys=True)` 的 UTF-8 字节计算。
- 两个格式化 JSON 文件的原始字节 SHA-256 均为
  `da3801bb6bd2e356899dccd602ec2bf0242129db7e0bd7263a0de5d0dd889b6e`。
  两种摘要用途不同，未将对象摘要误称为文件哈希。

新服务器上的全新 Chrome context 随后完成只读浏览器重开：1440 / 768 / 390 宽度下侧栏合理开合，
原结果可见，成员 Run 保持只读，没有 POST 或重放任务。真实截图为
`team-restarted-1440.png`、`team-restarted-768.png`、`team-restarted-390.png`
及 `team-restarted-member.png`，根代理已逐张打开检查。
`browser-reopen-log.txt` 字面输出：

```text
TEAM_BROWSER_REOPEN=PASS; COLD_PROCESS=NEW; INTAKE_REPLAY=NONE; WIDTHS=1440,768,390; MEMBER_RUN=READONLY; POST_REQUESTS=0; PAGE_ERRORS=0
```

本轮精确命令、原始哈希、整合失败对照与源码副本回滚结果统一记录在 `.naumi/transactions/pa06-teams-20261104/VERIFICATION.txt`；回滚结果以实际执行记录为准，不以脚本存在代替通过。
上一 peer 切片的 583/178、浏览器与冷重启记录只属于原 peer 事务，不继承为本轮团队证据。

### 审查修复与本轮变更说明

- **A / TUI 受理显示**：定位到已持久受理的 `admitted` 被当失败且隐藏编号；
  新增三个回归的基线为 `2 failed, 1 passed`、退出 1；
  修复后与 pi TUI 集中专项为 `6 passed, 3 warnings in 10.30s`、退出 0，ruff/import 退出 0。
  更广相邻模块保留 `2 failed, 62 passed`：lifecycle 的 running/standby 断言在原版 app 复现；
  expired foreign owner 交互断言仅一次时序敏感失败，原版定向调用通过，未证明同一原版缺陷。
  完整命令、日志和分类见 `team-tui-evidence.json`，不将这两项洗成通过。
- **B / 用量不确定性**：阶段可信分项缺失或与根 Job 总 token 不一致时不再视作零；
  有限 token 限额下阻断后续阶段，只设费用限额时保持 null 分项并按真实费用继续。
- **C / 授权范围一致性**：后端受理与执行重查统一为“祖先团队＋实际授权叶子”；
  前端允许用户移除停用叶子后继续，不解除团队祖先、缺失、循环或所选成员的校验。
  受控基线为后端 `4 failed, 14 passed`、前端 `8 failed | 4 passed`，退出均为 1；
  修复后后端合同模块 166 passed、前端全套 213 passed、build 通过，退出均为 0。
  精确命令、空输入、完整 stdout/stderr、退出状态与原件哈希见
  `scope-c-baseline-record.json`、`scope-c-modified-record.json` 和 `scope-c-evidence.json`。
- 真实模型和浏览器已完成的操作未因上述本地审查修复而重复执行；
  最终整合与统一副本回滚各自保留完整原始记录，未以本节专项结果替代。

### 最终集中回归与原始失败对照

- `ruff check src/` 与本轮个人测试文件检查通过；API/TUI/团队入口 import 通过。
- 最终真实团队集成与 headless TUI：**36 passed in 137.21s**，退出 0。
  其中覆盖缺失或不一致的可信 Run 用量、两种有限 token 限额、只设费用限额、无协调工具的停用叶子等边界。
- 较大相邻批次：**1465 passed / 6 failed in 337.61s**，退出 1；没有声明仓库全绿。
  六个原 nodeid 在 Git `8f8d4bf` 完整原始源码、原测试、同进程导入来源核验下全部复现：
  **6 failed in 12.62s**，原件前后 2076 文件哈希清单不变。
- 失败分别涉及旧 API lifespan 测试替身、WebSocket close 测试替身、旧产品直接构造入口、
  旧测试构造数量阈值、入口字符串计数和 TUI 生命周期状态；具体错误保留在
  `final-backend.txt` 与 `baseline-adjacent-record.json/.txt`。
- 两个新增 fixture 已改用 `create_agent_engine`，最终旧构造数量 **234** 与基线一致，未修改旧阈值。
- 前端最终 **25 files / 213 passed**；TypeScript + Vite build 通过。
- 验收服务器仅使用隔离数据，截图和新进程读回完成后已核验无活动团队任务并关闭，未更改用户应用进程。
- 完整 `pytest tests/ -x` 的独立执行记录见 `full-repository-record.json`；若遇失败或清理超时，保留原状态，不能算通过。
- 源码事务包含原始/修改后哈希、`MODIFIED_FILE.py`、`DIFF_FILE.patch`、`VERIFICATION.txt`、
  可执行 `ROLLBACK.sh`；回滚只在独立源码副本执行，不恢复用户数据库，真实工作区保留修改后实现。

## 8. 自我审视与后续项

1. 团队已具有真实规划、成员与汇总执行，不是目录按钮或角色提示词展示。
2. 当前已具有独立真实模型、HTTP 浏览器与新进程读回证据；整合与副本回滚按本轮独立记录验收，PA-06 因产物版本和私有 ACL 等未完项保持 partial。
3. 完整 `artifact_id/version` 交接、输入资料 ACL 和撤销分享属于后续切片；文件名或聊天正文不是产物版本。
4. 团队自己的持久对话不等于分范围语义记忆。跨工作区个人偏好、纠正/删除与召回仍由 PA-02 验收。
5. 个人事项、时间触发、送达、真实账户/外部动作、语音和多设备目标没有缩减或冒充已完成。
