# PA-01 事项 API、Tool 与人工命令接入

## 接入点

- `api/routes/items.py`：认证人工管理 API，路由前缀由 app 提供 `/api/v1`；事项 `/personal/items`，结果 `/personal/item-results`。
- `personal/item_tools.py`：`PersonalItemsTool(service)`，通过服务的真实 `engine` 解析当前可信 Agent。API 认证人工管理不传 `caller_actor`；Tool 必须传可信实际 actor。
- `personal/item_commands.py`：CLI、TUI、Web 命令共用 `/items` 解析与格式化。普通操作全部走 `engine.execute_tool` 和同一 Tool，不形成第二套执行实现。
- `main.py`、`cli/completer.py`、`api/routes/commands.py`：命令处理、帮助、补全、Web 命令目录与共享 SSE 路径。

## 输入与身份

1. 顶层和嵌套 JSON 拒绝未知字段；禁止模型或命令 JSON 输入 actor、caller、执行路由、人类可信通道等字段。
2. API 使用 strict Pydantic 类型。POST/PATCH 请求在进入模型前使用现有严格 JSON 解析，拒绝重复键、NaN 和超过 1 MB 的正文。`fold=True` 不会被当作整数 1 接受。
3. create Tool 通过 `runtime.require_current_agent` 重验身份、状态和 turn version；来源 session 从实际当前会话补入，不由用户 JSON 伪造。
4. request_id、expected_revision、原输入引用和完整嵌套值不会在接入层重新生成或改成另一请求。普通管理命令只生成一次 ToolCall 跟踪 ID，不生成写入幂等 ID。
5. API/Tool/CLI 分页上限统一 100。policy 支持部分覆盖；API `exclude_unset=True` 将省略字段原样交给服务解析，不提前注入假的默认权限。上限与服务一致：100 轮、100000000 微美元、3600 秒。
6. 实际权限、资料授权、workspace、只读 session、Agent 版本、CAS、UNKNOWN 与再次执行资格继续由共享 root service 和现有 Engine 权限层判定。

## 人工与模型分离

- 普通 `/items` 操作固定使用现有人工 `agent_name="cli"` 权限路径，因此 CLI 与 TUI 不因 frontend 名称不同而出现权限分叉；模型调用仍须实际分配 `personal_items`。
- Tool 不暴露 `read` 操作。`list/get/results/result/history/occurrences` 不确认人类已读。
- 唯有明确 `/items read-json` 调用服务 `read_result`，并以当前可信 actor、exact delivery ID / SHA-256、原 request_id 和服务器确定的 `cli` / `tui` / `web` 通道确认。
- Web GUI 的 read endpoint 硬编码可信 `web`；Web `/items` 命令由 `_CommandEngine` 硬编码其人工通道为 `web`。body 不接受这些可信字段。
- 人工命令只在真实 read receipt 和原 occurrence/delivery/hash 一致时输出「已确认指定结果已读」。错配回执保留原请求信息并显示待核对；Web 手工错误将真实 command Run 标为 failed，而不是 completed。
- `/items` Web 命令在发现 `personal_execution_id` 时，于新 Run 或消息写入前返回 409，保持执行历史只读。

## 命令示例

```text
/items list active
/items results unread 30 0
/items get pi_<32hex>
/items history pi_<32hex>
/items occurrences pi_<32hex>
/items pause-json '{"item_id":"pi_<32hex>","expected_revision":3,"request_id":"original-request"}'
/items read-json '{"occurrence_id":"pio_<32hex>","request_id":"original-read","delivery_id":"original-delivery","expected_delivery_sha256":"<64hex>"}'
```

## 集中验证与证据

基线命令：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_api_commands.py tests/unit/test_command_index.py tests/unit/test_personal_artifact_commands.py -q
# 100 passed in 14.23s; exit 0
```

修改后集中命令：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_personal_item_interfaces.py tests/unit/test_api_commands.py tests/unit/test_command_index.py tests/unit/test_personal_artifact_commands.py -q
# 181 passed in 13.18s; exit 0
```

检查：

```text
ruff check [本接入层 6 个源文件 + 专项测试文件]
All checks passed!
ITEM_INTERFACES_IMPORT=PASS; ROUTES=11
```

测试覆盖严格 API 解析、认证、所有动作的真实 ASGI 路径、精确 payload、全局错误传递、当前真实 runtime actor、缺失目录/停用/turn 版本变化、模型不暴露 read、原请求重试、人工 exact 已读回执、共享 CLI/TUI 调用、Web SSE 与 SQLite 会话/Run 持久化、执行 session 零写入、policy 部分覆盖。

额外使用真实生产 `create_agent_engine`、真实 Agent 目录/绑定/SQLite 和真实 `PersonalItemService` 验证：空工具 Agent 的 CLI 可以创建远期 2035 单次事项，TUI 与 CLI 查询一致，模型调用未分配的 `personal_items` 仍被拒绝，`tool_names` 仍为空。测试对任何模型调用设置断言；未启动 provider，也未执行该远期事项。

原件和 hash：`.naumi/transactions/pa01-20261104/integration-baseline-manifest.json` 与 `original/src/...`。最终文件 hash：`integration-files.json`。日志：`integration-baseline-tests.txt`、`integration-modified-tests-final.txt`。初轮巨大参数 ID 在 Windows 的 `PYTEST_CURRENT_TEST` 超过环境变量长度，已用短测试 ID 修正；该错误不是事项业务回归。

## 集成后续

接入层已使用真实 HTTP/CLI/Engine 管理路径验证；真实模型执行、Docker 输入隔离、真实报告投递、浏览器和冷启动恢复仍由 PA-01 整体集成验收记录，本专项不替代那些证据。
