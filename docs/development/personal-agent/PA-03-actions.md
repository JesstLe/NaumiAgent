# PA-03 日历草稿、确切审批与原对象对账

日期：2026-11-05  
基线：`281be9382e7fdb2e310c15e7d9e0f86e32e6c946`  
状态：动作域已实现，最终本地协议/SQLite/Job/Run、来源依赖与实时 update 预检组 **56 passed**；指定四文件 ruff 与 import 通过。**真实 Google 账户：NOT_RUN**。

本切片由 `CalendarActionMixin` 与 `CalendarActionExecution` 实现，复用 Root 的 `ConnectionAuthority`、`CalendarSyncMixin`、`ConnectionStore`、Vault、Google 协议 adapter。没有修改这些 owner 的实现，没有另建模型执行器或第二套 Run/Job 状态机。

## 1. 文件与共享 service

- `src/naumi_agent/personal/calendar_actions.py`
- `src/naumi_agent/personal/calendar_action_execution.py`
- `tests/unit/test_personal_calendar_actions.py`
- `tests/unit/test_personal_calendar_action_execution.py`

Mixin 接口：

```python
await service.list_actions(caller_actor=None)
await service.get_action(action_id, caller_actor=None)
await service.prepare_action(payload, caller_actor=None)
await service.revise_action(action_id, payload, caller_actor=None)
await service.approve_action(action_id, payload, trusted_channel="manual_api")
await service.reject_action(action_id, payload, trusted_channel="manual_api")
await service.reconcile_action(action_id, payload, caller_actor=None)
await service.action_tick()
await service.stop_actions()
```

`caller_actor=None` 是 service 内部可信人工入口，不是一个允许模型传入的参数。Tool 必须从当前真实执行身份传入 actor（包括 `main`）。模型 main 仅能查看自己的动作，不能凭主 Agent 名称读取其他 Agent 的私有草稿。人工列表可管理当前工作区的动作；跨工作区记录不可见。

prepare 的 actor_id 可省略；模型调用省略时绑定 caller，填写不同 actor 时拒绝。人工可明确选择已授权的目标 Agent。prepare/revise 都没有 human、approved、credential、issuer、subject、grant 或内部依赖字段入口。

## 2. 草稿与批准合同

prepare 支持 insert/update；body 严格走 `connection_models.action_body`，只包含 summary/description/start/end。后台才注入 `extendedProperties.private.naumi_operation_id`，不接受模型自己提供 marker。insert 的 client event ID 在草稿创建时产生并固定，使用 UUID hex，符合 base32hex；revise 不改变该 ID。

update 必须明确 target_event_id/base_etag，而且来源必须包含该原对象。Google adapter 执行实际 `If-Match`，412 不自动合并。日历、与会者、附件、会议或额外字段不进入本切片写合同。

草稿可来自只读连接；批准与实际派发必须有当前 can_write。写入升级与具体草稿批准始终是两次独立人工决定，OAuth 成功不生成批准、Job 或写入请求。

唯一的读授权连续性例外是：健康 connected 的纯 RO 连接，经完整 OAuth 重新授权，返回与原连接相同的已验证 issuer/sub、email 和 OAuth client，且新 scope 在语义上覆盖原读 scope。`calendar.events.owned` 语义覆盖 `calendar.events.owned.readonly`，不是 URI 字符串子集。此时只增加 token_generation、更换独立凭据 slot，保留 auth_epoch、完整 grants（含撤销状态）、镜像、同步游标和原草稿。原草稿仍须在有效期内由人工核对确切 revision/digest 后批准；原来源或分享撤销、Agent 升版、过期、已提交和 UNKNOWN 状态不会恢复。

其他重新授权（包括 email/client 变化、同级重授权、降权、断连、已知凭据或授权异常）继续增加 auth_epoch 并清空 grants；旧草稿、批准、历史 ActionRef 和来源不会被新授权替换。issuer/sub 不同仍拒绝接纳，不覆盖原账户。上述窄分支保留原连接 revision CAS 和 account lease，OAuth 等待/交换期间的撤权或状态变化使旧 intent 失败。

激活事务还检查原 RO epoch 中的旧写事实：存在 approved/dispatching/submitted/unknown，或 draft 已携带 approval/execution，均不走连续分支而执行 fencing。超出有界 Action 检查数量也不保留权限。更早已 fencing epoch 的 UNKNOWN 保持原状态与证据，不阻止当前健康读权限升级，也不因升级恢复执行。

approve payload 必须同时包含：

```json
{
  "request_id": "stable-request-id",
  "expected_revision": 1,
  "expected_digest": "the-digest-frozen-when-the-human-reviewed-the-draft"
}
```

缺任何一项、摘要/版本变化、非 manual_api/manual_cli/manual_tui 渠道都会拒绝。批准不能由 BYPASS、普通工具 allow 或模型 human=true 生成。reject/reconcile 使用 request_id + expected_revision。

digest 的不可变核心包含：action ID、草稿版本、workspace、connection、原 issuer/sub、auth_epoch、actor/version、确切 grant、operation/原 event ID/base ETag、带后台 marker 的正文、Calendar source refs、私有 artifact/memory dependencies、有效期和来源会话 ID。有效期必须在未来 24 小时内。

批准保存人工 receipt、原核心摘要、版本、时限和渠道。批准后不能 revise，也不能给 dispatching/submitted/unknown 重新签发批准。需要新决定时不改写原未知行动或原身份；未知动作只对账。

## 3. 防止省略来源与私有上下文漂白

模型即使填写 `source_refs=[]`，也不能抹掉它实际读取的私有上下文。service 在模型入口从权威上下文冻结：

- `engine._personal_artifact_refs`；
- `engine._personal_memory_artifact_refs`；
- 当前绑定 Session 的持久 artifact refs；
- `engine._personal_memory_runtime_refs`；
- `personal_memory_runtime.item_id()` 返回的原事项范围。

这些字段来自 runtime，不来自 payload。人工 UI 调用不会继承此时另一轮模型正在使用的无关 Session。

动作 DTO 顶层增加只读：

```text
artifact_refs: [{artifact_id, version, sha256, grant_id, grant_revision}]
memory_refs: [{memory_id, revision, content_sha256, grant_id, grant_revision}]
memory_item_id: string | null
last_error_code: string | null
```

actor 统一为 Action.actor_id。它们不是可编辑参数；正文、来源 chips、账号及版本一起被 expected_digest 绑定。artifact 用确切版本与 grant，memory 用原 revision/content hash/grant；不以 latest 或新 grant 替代旧引用。

last_error_code 是只读诊断码，不是依赖或正文，不参与批准摘要。只公开最长 64 字符的受限小写标识符；意外非 code 值替换为 action_error，绝不把 raw exception/provider message 回传。前端按明确中文映射展示权限、证据恢复或 UNKNOWN 原因，不把 not_found 误译成“未受理”。

revise 只能并入旧依赖，不能因下一轮 query miss 或新会话空上下文丢掉旧来源。来自不同事项的记忆不被重新标记到另一个 item；明确拒绝这种合并。批准、模型 get、派发及模型对账都重新授权 artifact refs 和 memory refs；artifact 的当前授权链进一步走 Root 的 ancestry→connection_sources 检查，覆盖日历来源、派生产物、Session 和 PA-02 引用。记忆纠正/删除或任一分享撤销后，原批准不能继续写入。

原依赖也写进加密 Job payload 的 context，并被 request/context hash 和 action digest 双重绑定。没有把来源正文塞进未加密 Job 身份字段。

## 4. 接入既有 Job/Run，而不是另造执行器

人工批准事务先持久化精确 execution intent：固定 payload/request、Job ID、Session ID、Run ID、namespace、prepared hash。只有其原件落盘后，tick 才触碰 AgentJob admission。

执行器直接使用：

```text
engine.personal_collaborations.jobs
engine.session_store
engine.chat_run_store
```

它与 personal-engine-v1 工作区共享真实 AgentJob 数据库、容量/FIFO、claim epoch、lease、mark_running 和机械终态。没有调用 Router、临时模型 Agent、容器或 ExecutionPort。确定性的账户 HTTP 写入本身不消耗 LLM。

每次执行保留独立 Session marker、真实 Run、授权/提交/读回步骤、CompletionReceipt，以及原 Job publication/inbox。成功的 Job result 计数为实际 0 tokens/0 cost/0 model turns。Run.usage 保持 None，因为现有 RunUsage 的 authority 是 engine_counter_delta；本模块不会伪造该来源。步骤和终态证据明确 `model_calls=0`、`usage_kind=deterministic_protocol_no_model`。

领域 conflict 映射为既有 Run error，领域 rejected/expired 映射为 Run cancelled；verified 对应 completed，结果不明保留 unknown。不会将草稿的领域状态扩展成另一套 Run 状态机。

等待人工批准不占 Job 容量；批准后 admission 可排队。实际 HTTP 之前，依次核对当前 epoch/grant/actor/source/expiry、原 Job request/owner/claim epoch/live lease，并持久化一次 dispatching。写入使用固定 event ID 或原 update target，永不改 ID 重发。

所有生产 Calendar HTTP 路径使用 token_snapshot 取得访问 token 与其确切 connection/token_generation 快照，不在 token 返回后读取较新连接并冒充原请求来源。刷新返回其原子保存的新 generation。执行器在写前核对快照；同步的旧 token HTTP 失败仅能影响原 generation，不能 fencing 或覆盖后来通过 OAuth 验证的新 generation。读取结果仍以读 epoch、账户和确切来源授权校验；凭据刷新本身不是模型提案或历史引用授权。

## 5. 跨库空隙与恢复

1. admission 之前，action 事务先把 execution.admission 从 prepared 改为 attempted。
2. 如原 Job 已存在，只接纳精确匹配的原 request，继续同一个 claim；不另造 request/Job。
3. attempted/bound 但原 Job 缺失时，保持 unknown，不重新 admit。即使随后批准到期，也不能把这个原受理缺口降成“未受理”。
4. shared capacity 明确拒绝且原 Job 未建立是已知本地事务结果，才允许回到 prepared 等待下次 tick；一般存储异常不这样处理。
5. dispatching/submitted 的旧执行者仍持有 lease 时，新 service 不抢执行；lease 失效后经 AgentJob 原始 receipt fence 收口 UNKNOWN，只读持久原件，不再 POST/PATCH。
6. source/权限在尚未 dispatch 的边界明确失效时，记录 cancelled/expired；该写入有当前状态条件，不覆盖其他执行者已进入 dispatching 的动作。

后台 task result 会被实际读取。失败观察写入 action 的 execution_observation，带安全 error_code/exception_type/时间；存储失败时不丢弃待观察 task，下一轮继续暴露。结果/运行结束/投递故障不擦掉已经存在的 provider 原对象验证证据。publication 已投递而 action binding 回填中断时，沿原 inbox receipt 补回，不重复投递。

executor.stop 与 tick 序列化；成功收齐 task 观察后，stop_actions 移除旧 executor。Root 同一 service 对象 stop/start 后会新建执行器，不会被旧 _stopping 标记永久阻断，也不会覆盖旧 Job/Run 原件。

## 6. 外部写、读回和 UNKNOWN

正常路径：

```text
draft → 人工 exact approval → approved/真实 Job 排队
→ Run + lease → dispatching（已持久）
→ 一次 insert/update → submitted
→ GET 原 event ID + marker/批准字段投影一致 → verified
→ 原 Job 结果 publication → inbox
```

- 2xx 仅表示已提交；实际原对象读回之前不显示 verified。
- 409 也必须 GET 原 ID：批准字段及 marker 一致才 verified，不一致为 conflict，404 仍 unknown。
- 412 为 conflict，不自动合并或重写。
- timeout、5xx、redirect、断连、取消、dispatching 后崩溃或缺 receipt 均保留 unknown，不自动重发。
- GET 404/403 不证明原写入不存在；断连后不再 GET。
- 原 issuer/sub 重新授权后，只有人工明确的 reconcile 可以跨新 epoch 对账；模型不能借新授权自动继续旧动作。
- 对账只 GET 原 ID，不修改原 approval/subject/epoch。后续 action 可凭新的确切 readback receipt 变 verified，但原 UNKNOWN Job/partial Run 不被假改为 completed；原 attempt 的事实保留。
- 撤销在请求发出后不能倒转已经发生的外部效果；任何补偿删除必须是另外的人工批准动作，而不是“源码回滚”。

服务端稳定请求回执覆盖 prepare/revise/approve/reject/reconcile；响应丢失原样重试时读原 action，即使后来过期也不重新签发批准或派发。相同 request ID、不同语义报 idempotency_conflict。

### update 的实时写前证明

仅检查本次 PATCH body 没有 attendees 并不足够：原目标可能已有参与者、附件、会议或周期字段；sendUpdates=none 也不是绝无通知的保证。sync 的 normalize_event 会丢弃这些扩展字段，不能作为写入时的安全证明。

因此 update 在领取 write dispatch authority、持久 dispatching **之前**必须：

1. 在当前账户/grant/source/Job lease 下，实际 GET 原 target ID 的 raw 对象；
2. 核对 ID 与 base_etag，拒绝 cancelled、已有 attendees/attachments/conferenceData/hangoutLink/recurrence/recurringEventId 及非 default eventType；
3. 检查普通对象形状，保存只含对象 ID/ETag/hash/Job request+claim epoch 的 preflight 证明，不保存 raw 扩展正文；
4. 重新核对授权与 Job lease，再以该执行的 preflight 证明进入 dispatching；
5. PATCH 仍使用批准的原 If-Match，封住 raw GET 后另一方修改的窗口；返回 412 只记录冲突，不重发；
6. 成功后再次 GET 原对象，核对 marker 与确切批准投影，才能 verified。

稳定码：

| code | 本次事实 |
|---|---|
| preflight_unsupported_target | 原目标包含未批准扩展能力或已取消，0 PATCH |
| preflight_etag_conflict | 原 ETag 已变化，0 PATCH |
| preflight_read_failed | raw GET 未完成/不可核验，0 PATCH |

这三项形成 Action conflict、既有 Job/Run error；原受理与审批事实仍在，并非“请求未受理”。有既存 dispatch 标记的 attempt 绝不套用这类 0 PATCH 结论，仍保留 UNKNOWN。Run 单独记录 update preflight 步骤和唯一 dispatch 阶段，失败也有真实阶段证据。

## 7. 本地测试与当前验收级别

测试分层：

- authority tests：真实 ConnectionStore SQLite、CAS/request/history/lease；受控 actor/source/material authority，不接触任何 provider。
- execution tests：真实本机 HTTP server + Forward transport，真实 Google adapter、ConnectionStore、加密 AgentJobStore、SessionStore、ChatRunStore、CompletionReceipt、publication/inbox。没有用模型或假 Run 完成结果。
- 原 Google OAuth/Calendar 组件的签名与协议测试另见 `PA-03-google.md`。

覆盖 exact digest/revision、人工渠道、actor/grant/source/epoch/expiry 变化、只读草稿、不可编辑字段、请求恢复、私有上下文省略/撤权/跨轮依赖、并发批准/worker、容量/FIFO、If-Match、409/412、5xx/丢回包、发送前/后的撤权、同账户人工只读恢复、跨库缺 Job、独立 service 读回恢复与后台异常观察。

首个动作组 `5 failed, 24 passed in 38.55s`：真实 Run→Job 收口暴露 `connection_models.canonical` 不接受 CompletionReceipt 的 tuple 容器。修复为仅在回执 JSON 边界规范化，而不修改 Root 的 strict canonical。扩充恢复/冲突/后台观察后 `35 passed in 47.90s`。同时自审将 admission 的原 Job 查证放到 lease 内、expiry 判断前，保护 UNKNOWN 语义。

最终验证命令：

```powershell
.\.venv\Scripts\ruff.exe check src/naumi_agent/personal/calendar_actions.py src/naumi_agent/personal/calendar_action_execution.py tests/unit/test_personal_calendar_actions.py tests/unit/test_personal_calendar_action_execution.py
.\.venv\Scripts\python.exe -m pytest tests/unit/test_personal_calendar_actions.py tests/unit/test_personal_calendar_action_execution.py -q --tb=short
```

最终来源闭环扩充组（exit 0）：

```text
All checks passed!
56 passed in 80.82s (0:01:20)
CALENDAR_ACTION_COMPONENT_IMPORT_OK
```

导入命令（exit 0）：

```powershell
.\.venv\Scripts\python.exe -c "from naumi_agent.personal.calendar_actions import CalendarActionMixin; from naumi_agent.personal.calendar_action_execution import CalendarActionExecution; print('CALENDAR_ACTION_COMPONENT_IMPORT_OK')"
```

来源漂白组为 `42 passed in 55.50s`，领域状态映射回既有 Run 后 `42 passed in 53.33s`；补同一 service stop/start 后 `43 passed in 57.21s`。Root 随后指出现有 update 只有写后核对：新增真实本机 HTTP 对照得到 `7 failed, 19 deselected in 18.29s`，参与者/附件/会议/周期/取消/旧 ETag 七项都在要求 0 PATCH 处观察到 1 PATCH。修复实时 raw GET 门后，扩充 legacy conference、特殊事件类型、GET 403/404/503、GET 后 ETag 变化 412，以及公开安全错误码，得到上述最终 56 项。该失败对照证明的是本地协议行为，不宣称真实 Google 账户已经发生过此问题。

测试中的恢复是独立 service 实例对真实 SQLite/原 Job/Run 的恢复，不宣称已经完成应用进程冷启动或实际账户重启验收。Root 新增 Tool Action 正文→确切历史 material/session pin 的来源链由相应 owner 联合验证，本页不继承其结果。

**没有真实账户或部署验收**：本页不证明实际 Google consent、真实账号 grant、日历写入/撤销或产品已部署。完整 API/Tool/CLI/TUI/Web2、真实 Artifact/Memory ancestry 联合验证、源码副本回滚与真实账户原对象验收由 Root 统一记录；这里的控制型 authority fixture 不冒充它们。

## 2026-11-05：读授权连续扩权与请求凭据快照

本节是前述 56 项组件批次之后的独立修复，不将不同批次数量相加。改动包括窄 RO→RW 连续性分支、同 epoch 旧写事实的激活门、token_snapshot 与刷新保存结果绑定、三个生产 HTTP 调用接缝，以及同步旧代失败的 generation fencing。

封存原 `_finish_oauth` / `_run_sync` 在独立 pytest 子进程回放，得到 `2 failed, 53 deselected in 6.05s`（exit 1）：纯扩权使 epoch 从 1 变 2；旧 token 的 401 又把新连接从 connected/epoch 2 改成 reauth_required/epoch 3。回放只加 token→snapshot 的协议接缝，不覆盖工作区源码。

首个修复后联合组为 `27 failed, 169 passed in 257.20s`（exit 1）。27 项均在 Action 执行夹具：旧连接种子缺 client_id_digest/token_generation，新增绑定校验在任何 HTTP 前停止执行。补齐种子两字段后，生产字节保持不变，集中六模块得到 `196 passed in 288.70s (0:04:48)`（exit 0）：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_personal_connection_service.py tests/unit/test_personal_calendar_sync.py tests/unit/test_personal_calendar_backoff.py tests/unit/test_personal_calendar_actions.py tests/unit/test_personal_calendar_action_execution.py tests/unit/test_personal_connection_integration.py -q --tb=short
```

该批次涵盖健康纯扩权、完整 grants 与 cursor 保持、旧分享撤销/替换不复活、确切来源与历史 ActionRef、email/client/status/降权差异、OAuth 并发撤权/CAS、旧 token 401/403/429/503、刷新 generation、旧 epoch UNKNOWN 不重放，以及既有真实本地 HTTP/SQLite/Job/Run/Artifact/Memory 链路。两轮执行前后均核对源码未漂移；最终组核对 12 个相关输入路径，`source_drift=false`。ruff 及 `CONTINUOUS_AUTHORITY_IMPORT_OK` 均 exit 0。

八路径封存原件、修改副本、差异、回滚脚本与演练结果集中于 `.naumi/transactions/pa03-read-continuity-20261105/`，以该目录的 `VERIFICATION.txt` 和 `MANIFEST.json` 为本域证据；后续 Root 的全量/浏览器/应用冷恢复批次独立记录。`LIVE_GOOGLE_ACCOUNT_ACCEPTANCE=NOT_RUN`。
