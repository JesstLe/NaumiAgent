# PA-02：可信记忆 API、模型工具与人工命令接入

日期：2026-11-04。模块基线：`4f7d86a451cd38b5fbc84cf8e090fac7768ee63d`。

## 实现

本接入层只绑定 `engine.personal_memory`，不读取旧全局 Chroma，不创建另一份记忆状态。
确认、修订、删除、授权和来源的事实权威由同一 `PersonalMemoryService/Store` 拥有。

### 模型工具

- `PersonalMemoryTool(service)` / `personal_memories` 仅公开 `list/get/search/propose/history`。
- actor 由真实 `runtime.require_current_agent()` 解析，模型参数不接受 owner、actor、caller、可信通道、
  source-role 或人工确认字段。
- 全部操作调用同一 `service.dispatch(..., caller_actor=actual_actor)`。
- 工具输出严格为 `personal_memory_refs` envelope。原记忆正文由模型发送边界实时授权解析；接入层
  即使收到错误扩大的 Service DTO，也不把 content/source/旧正文写进 Tool history。
- `memory_store` 兼容别名只提出工作区候选，等待人工确认；未提供 request_id 时由 Service 绑定实际
  Run 和原 payload，不在别名中随意重造请求身份。
- `memory_recall` 兼容别名保留 category 与 top_k，结果仍为确切引用。别名保留只读、并发安全元数据。
- 候选请求重放根据当前状态回复：已确认、已删除、已拒绝或已替代都不再宣称新建待确认候选；
  缺少原候选身份或状态时返回可核对错误，不推测创建成功。
- 本地语义索引未就绪时，工具保留明确的固定诊断及安全计数，不把索引错误伪装成确定没有记忆；
  原 error 正文与来源正文不进入工具回执。

### 人工命令

`main.py` 和 `cli/commands_meta.py` 的旧 `/memory` 入口均转向 `run_memory_command()`。
旧的 stats/search/clean/export 辅助函数也统一转发，未留下直接访问旧 Chroma 的分叉。

```text
/memory stats
/memory list [personal|workspace|item] [搜索词]
/memory get|history|grants <记忆ID>
/memory search <查询>
/memory export [personal|workspace|item]
/memory legacy [每页数量] [偏移]
/memory clean|consolidate|index-sync
/memory <create|propose|confirm|revise|delete|reject|share|revoke|import-legacy>-json '<JSON对象>'
```

stats 查看当前上下文与索引状态；clean/consolidate 只同步派生索引，不合并或删除用户事实。
export 输出当前授权范围的 JSON，GUI 可以明确下载；命令不再自动把私有内容写到共享工作区的
`memory_export.json`。额外路径参数明确报错，不悄悄忽略。

人工命令默认使用当前真实 Agent。CLI/TUI 通道固定为 `manual_cli`，Web 固定为 `management_web`，
两者都不从 JSON body 读取。写入保留原 request_id、CAS revision、content SHA 和 exact active ref。

### Web 命令持久化

`/memory` 已进入共享 Web command index。只读执行 Session 在任何新消息或 Run 写入前返回 409。
人工命令失败会将真实 command Run 记录为 failed，而非默认 completed。

当次 SSE 可以向人工显示实际正文；持久 assistant 消息和 Run step 仅保存引用 envelope，
`metadata.personal_memory_refs` 来自实际 Service result 的 exact refs。不会把一次检索得到的旧正文
保存成以后模型可反复读取的 Tool/command 输出。实际用户输入的命令仍属于独立对话历史。
两个 Web adapter 的引用暂存彼此隔离，不在共享 Engine 上交叉借用。

### API

13 个路由对象均位于 `/api/v1` 下，采用现有认证机制：

```text
GET  /personal/memories/context
GET  /personal/memories
GET  /personal/memories/search
GET  /personal/memories/export
GET  /personal/memories/legacy
GET  /personal/memories/{memory_id}
GET  /personal/memories/{memory_id}/history
GET  /personal/memories/{memory_id}/grants
POST /personal/memory-requests
GET  /personal/memory-requests/{request_id}
POST /personal/memory-requests/{request_id}/apply
POST /personal/memory-requests/{request_id}/discard
POST /personal/memories/{operation}
```

认证人工管理可以选择实际 actor；这个管理身份不暴露给模型 Tool。
prepare/apply 保留同一 opaque request identity，内外 request_id 要一致；GET request 不返回原始
prepared payload。apply 只接受 expected_fingerprint，不接收第二份正文或可信字段。

用户放弃已准备请求时，discard 使用同一严格 fingerprint body、原 request_id、人工 actor 和固定
`management_web` 通道调用 `service.discard_request()`。Service 擦除服务端未应用草稿后返回 erased；
若原请求已经 applied，则保留已提交事实并返回 applied 元数据，不把丢弃草稿伪装成删除事实。
discard 不向模型工具开放，也不允许 body 替换 actor、正文或可信通道。

列表状态同时支持 `conflict`，与 Service 的冲突候选过滤一致；未知状态在 HTTP 边界拒绝，
不把冲突页请求转成空列表或吞掉筛选条件。

请求在进入 Service 前拒绝重复 JSON 键、NaN/Infinity、超过 1 MiB 的实际 body、未知字段、伪造
source/confirmation/owner/caller/channel。所有成功响应不缓存；认证错误也设置 no-store，参数错误
使用中文领域错误，由统一 PersonalAgentError handler 返回。

新检索入口上限为 20 条、查询 2000 字；list 上限为 100 条。旧 memory_recall 别名维持原
top_k<=10 / query<=1000。create/propose 的 topic<=120、content<=8000，输入长度不静默截断。

## 验证记录

基线：

```powershell
E:\Workspace\NaumiAgent\.venv\Scripts\python.exe -m pytest tests/unit/test_api_commands.py tests/unit/test_command_index.py tests/unit/test_memory_tools.py tests/unit/test_cli_commands_meta.py -q --tb=short
```

字面结果：`46 passed in 6.92s`，exit `0`。

接入层集中组在相同基线输入外增加 `tests/unit/test_personal_memory_interfaces.py`。
覆盖实际 ASGI 路由、真实参数解析、runtime actor、模型管理操作隔离、alias 候选、引用型回执、错误/超长
输入、opaque prepared/apply、CLI/TUI 共用入口、实际 SQLite Session/Run 的 Web SSE 及正文不持久化。
底层 Service 使用明确的受控 port；这不是 SQLite 记忆权威、embedding 或真实 provider 验收声明。

首轮 `138 passed in 7.01s`。对齐实际 Service 的检索上限、candidate_id 和安全诊断后，
集中组 `141 passed in 7.46s`；候选重放状态、元数据及缺失身份检查补齐后的最终集中组结果：

```powershell
E:\Workspace\NaumiAgent\.venv\Scripts\python.exe -m pytest tests/unit/test_personal_memory_interfaces.py tests/unit/test_api_commands.py tests/unit/test_command_index.py tests/unit/test_memory_tools.py tests/unit/test_cli_commands_meta.py -q --tb=short
```

字面结果：`147 passed in 6.55s`，exit `0`。stdin 为空；完整逐行输出与执行命令分别保存在事务
`interfaces-final-state-output.txt`、`interfaces-final-state-record.json`。首次冻结的限定 Ruff/import 与
原件哈希复核见 `interfaces-completion-record.json`；其后新增 discard 和 conflict query 的增量验收
单独记录，不覆盖先前执行证据。

discard 增量覆盖 erased/applied 回执、固定人工通道、原身份与 fingerprint、缺失/错误类型/未知
字段、重复 JSON 键、NaN/Infinity、超大 body、认证拒绝、下游冲突及模型操作隔离。增量组首次结果
为 `169 passed in 7.48s`。补齐实际 `state=conflict` HTTP 查询后，最终同一集中命令结果为
`170 passed in 7.20s`，exit `0`，stdin 为空；命令和逐行输出分别见
`interfaces-discard-conflict-final-record.json`、`interfaces-discard-conflict-final-output.txt`。
此处确认的是实际 HTTP 接入和受控 Service port 合同；服务端草稿真正擦除、apply/discard 并发
与已经提交事实不被取消的 SQLite 验收仍由 Root/Store 负责。

### 真实 provider 的 schema 修复验收

真实模型首先完成 A/B 跨工作区和 Echo 未分享/确切分享/撤销后的 5 个独立会话。
第 6 个实际工具请求被 provider 以 HTTP 400 拒绝：`properties.state` 的 enum 缺少显式 type。
原 Run 保留为 failed，原 checkpoint/费用未知状态未覆盖，也未自动重发。

本次统一为所有 memory 工具及 aliases 的 enum 增加 `type: string`，并把 Python tuple 投影为
原生 JSON array；允许值集合不变。新增递归模型 schema、Draft202012 metaschema、原有效/无效
输入等价性及回执生命周期测试。最终接口集中组 `201 passed in 9.96s`，exit `0`，记录为
`interfaces-schema-repair-fixed-record.json`。

Root 明示授权后，先只读确认原 Run 已终态、单一 400、无 Tool action/候选，再运行新的独立验证：
新 Run `c7e7f7141af1` 完成两次真实 provider 调用和一次自主 `personal_memories.propose`。
实际 SQLite 候选为 proposed、confirmation=null、来源为 assistant；随后本地删除与原请求重放
返回 deleted，旧 active ref 撤权，payload/FTS 均为 0 行。没有重跑前 5 个会话或原失败请求。

逐场景实际输入 SHA、transport response ID SHA、usage、Run/Tool/SQLite 证据见事务
`LIVE_MEMORY_VERIFICATION.md`、`live-memory-first-readback.json`、
`live-advice-schema-repair-result.json`。provider 完整回执与 prepared context artifact 分开记录；
没有把 Engine token usage 或估算费用当成提供方账单证明。

原件与 SHA：`.naumi/transactions/pa02-20261104/interfaces-baseline-manifest.json`。
当前 9 个文件与 SHA：同目录 `interfaces-files.json`。原件保留在 `original/src/...`，未覆盖。

## 接线与剩余验收

Root 负责在 app 注册 `memories.router`，在 Engine 注册 `PersonalMemoryTool`、`MemoryStoreAlias`、
`MemoryRecallAlias`，以及 Service/Store/Index、真实运行时按引用重新解析和私有资料边界。
初始接入实现没有自行启动服务/provider；上述独立真实 provider 验收仅在 Root 明示授权后运行。
本接入层没有提交，也没有把 API/受控 port 测试当作全产品完成。
真实记忆范围、索引、模型、GUI、冷重启及源码副本回滚由主流程统一收口。

## 可复用命令示例

```text
/memory create-json '{"request_id":"preference-language-001","topic":"回答语言","category":"preference","content":"使用简洁中文回答","scope":"personal"}'
```

确认/更正时使用 GET 返回的实际 memory_id、revision 和 content_sha256；示例不伪造这些实际标识。
