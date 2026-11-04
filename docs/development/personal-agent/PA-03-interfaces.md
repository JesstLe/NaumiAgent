# PA-03 API、模型工具与人工命令

## 共享服务边界

三条入口共用 `engine.personal_connections`；不创建第二 Engine、不切会话、不获取 Engine 大锁。注册 router、Tool 与 CLI/TUI 路由由 Root 的集成改动负责，本域不修改 app/main/engine/tui。

严格 payload models 集中于 `connection_tools.py`，API、Tool 与命令复用；Pydantic strict + extra forbid，嵌套对象同样拒绝额外字段。API只允许冻结前端路径，128 KiB JSON 正文；拒重复key、非有限数字、未知字段、身份伪造字段与类型强制转换。

## 人工 API

`api/routes/connections.py::router` 提供 `/personal/connections` 和 `/personal/actions` 全部管理路径。必须已配置运行时 api_keys 且通过 X-API-Key 或 Bearer 请求头认证；拒 anonymous 和 query api_key。认证先于正文解析，不把 secret 放入 URL。

成功、认证、校验、服务错误均 `Cache-Control: no-store` 与 `Pragma: no-cache`。校验错误只返回固定中文文案，不返回 Pydantic input、原始异常或正文秘密。

只有内部路由注入 `manual_api`：client、OAuth、取消、sync、disconnect、grant/revoke、event-to-item、approve/reject。body 不接受 trusted_channel、human、caller_actor、owner_key 或 actorcredential。actor_id 仅在冻结契约允许的“目标 Agent”字段出现，不代表当前操作者身份。

人工 action list/get 不传 caller_actor，表示服务内部可信人工入口；模型包括 main 必须传实际绑定 actor。

## 模型工具

- `PersonalConnectionsTool`：context、list、events、sync、event_to_item。
- `PersonalActionsTool`：prepare_action、get_action、list_actions、reconcile。
- 每次执行均重新调用 `require_current_agent(service.engine)`，不缓存身份；schema 顶层与嵌套 payload 拒 actor_id/caller/trusted_channel/owner 等伪造身份。
- events 只调用 `tool_events()`，由真实 service 保存来源 artifact 并 pin 当前 session 后返回；不走人工 `events()`，避免撤权旧正文旁路。
- Tool 不提供 connect/configure/grant/disconnect/approve/reject。提案不会变成人工批准，generic tool confirmation 不签发账户权限。

```json
{"operation":"events","connection_id":"pc_11111111111111111111111111111111","limit":20}
```

```json
{"operation":"reconcile","action_id":"ca_33333333333333333333333333333333","payload":{"request_id":"original-request","expected_revision":7}}
```

## CLI/TUI 共享协议

`parse_connection_command` / `execute_connection_command(service, arguments, trusted_channel=...)` 以及对应 action 函数供 CLI/TUI 共用，人工入口分别注入 `manual_cli`、`manual_tui`。

```text
/connections context|list
/connections client <path>
/connections connect [id] [--write]
/connections events <id>
/connections sync <id> <revision>
/connections disconnect <id> <revision>
/connections grant <id> <revision> <actor> <actorVersion>
/connections revoke <id> <revision> <actor>
/connections request <id>
/actions list|get <id>
/actions propose <JSON>
/actions approve <id> <revision> <digest>
/actions reject <id> <revision>
/actions reconcile <id> <revision>
```

client 文件有限读取 16 KiB，不缓存/另存明文。Windows 路径保留反斜杠与空格。connect 原 ID 时先读取当前原 revision，再让服务 CAS 验证，之后不自动重试写入。disconnect 命令默认仅本地断开，remote_revoke 为 false；显式远程撤销通过账户管理 API 的布尔字段选择。

人工 `/connections events` 同样保护后续模型上下文：使用当前 session，没有时先 get_or_create_session；根据 require_current_agent 派生实际 actor，先调用 tool_events 对本页50条精确来源 pin，再读同 actor 的可读页。两页 source_ref 必须一致，且读取后 session/actor 不变；pin失败、撤权或同步期间版本变化不会返回未pin正文。Agent 对话不静默回退 main 账户权限。

每个新人工 mutation 只生成一个 request UUID，输出 `{'request_id': 原编号, 'result': 结果}`。propose JSON 已有 request_id 则原样保留。执行器没有自动 mutation retry；异常附相同原 request_id，未知结果只供 `/connections request <原ID>` 核对，不另造请求掩盖状态。

## 验证与阶段

完成本域代码与测试后集中验证真实 ASGI 路由、fixture 服务端口、真实本地 SQLite receipt/CAS、Tool 身份派生与事件 pinning 端口、人工共享 parser/executor。fixture 服务仅证明 adapter 分发合同，真实 SQLite 仅证明本地持久化路径，不宣称 Google live OAuth / 日程写入或 UI 集成验收。

### 实际集中验证

```text
.\.venv\Scripts\python.exe -m ruff check src/naumi_agent/api/routes/connections.py src/naumi_agent/personal/connection_tools.py src/naumi_agent/personal/connection_commands.py tests/unit/test_personal_connection_interfaces.py
All checks passed!
exit_status=0

INTERFACES_IMPORT_OK
ROUTES=20
exit_status=0

PYTHONIOENCODING=utf-8
.\.venv\Scripts\python.exe -m pytest tests/unit/test_personal_connection_interfaces.py -q --tb=short
99 passed in 3.69s
exit_status=0
stderr=""
```

第一整域为79 passed；追加严格分页、嵌套extra、重连身份配对、原request ID、工具pin失败无fallback与命令边界后94 passed；采纳人工events会话pin后最终99 passed。没有用小happy-path结果替代完整域测试。

本次最终源码/测试 SHA256：

| 文件 | SHA256 |
|---|---|
| api/routes/connections.py | `732A21DFE000E8D604ADBFCD14A5F01F526146327CCB491E23F4231A20534548` |
| personal/connection_tools.py | `E449C2883D234AFC214516B9C2AE4B91A9D2435C0BBD870316F321B49278880D` |
| personal/connection_commands.py | `D270C6A0D2FE709241B5BF3F2C2ED5AF623B301C4D6E44AC1B01B4B25F454B52` |
| tests/unit/test_personal_connection_interfaces.py | `FE12CAEAABE1AF06B6474A0D6946D152DC0E870D59764A1F99381632AA0A4DC6` |

完整 argv、空stdin、stdout/stderr、exit与执行前后hash记录在 `.naumi/transactions/pa03-calendar-20261105/INTERFACES-record.json` / `INTERFACES-output.txt`。94通过的中间态另存 `INTERFACES-intermediate-94-record.json` / `INTERFACES-intermediate-94-output.txt`，与最终接口源码明确区分。

覆盖实际ASGI的强身份认证、管理渠道/目标Agent字段、冻结前端路径、重复JSON/非有限数/128KiB容量、布尔强制转换、精确approval摘要、no-store成功与错误、异常秘密脱敏；Tool每次实际身份派生、main模型不等于人工None、源pin专用端口与失败不旁路；manual CLI/TUI同parser/executor、稳定request ID、未知结果无自动重发、Windows配置路径与限量读、人工events会话创建/实际actor/先pin/来源或session变化拒绝展示。

真实ASGI→fixture服务→实际SQLite的重复请求/异正文冲突也通过，但fixture服务只证明adapter和本地事务集成。router/Tool/命令生产注册、真实来源artifact/pin实现、Root service与动作service、Google live授权与日程读写仍由总集成分层验收，当前不声称这些已由接口fixture证明。
