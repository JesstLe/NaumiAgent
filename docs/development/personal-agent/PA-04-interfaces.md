# PA-04：周期计划 API、Tool 与 CLI/TUI

日期：2026-11-05

## 模块职责

本接入层不另建执行引擎、不扩展 PA-01 的单次时间合同。所有操作调用现有 `PersonalRoutineService`；自然语言任务由 Agent 转为明确的结构化计划，经过真实时间预览、摘要确认和次数/估算额度约束。

本模块文件：

- `src/naumi_agent/personal/routine_tools.py`
- `src/naumi_agent/personal/routine_commands.py`
- `src/naumi_agent/api/routes/routines.py`
- `tests/unit/test_personal_routine_interfaces.py`

引擎构造/注册/生命周期、主 CLI、补全、API app、运行时工具允许列表由 PA-04 主集成负责，详见 [PA-04 主记录](PA-04.md)。前端合同与交互见 [PA-04 UI 记录](PA-04-ui.md)。

## 严格共享输入

API 请求和模型 Tool 使用同一组 Pydantic strict/extra-forbid 模型，Tool JSON Schema 由这些模型生成。Raw definition 的最终字段与限制验证还调用 `routine_models.raw_definition`，不在接入层创造另一套可执行定义。

- 原始 schedule 为 once/cron/trading_day discriminated union。
- 只接收原始时间输入，不接收 public version、timezone_sha256、calendar_sessions、route 或 source_context。
- 数字字符串、浮点修订号、bool 充当整数、字符串充当 enabled 均不转换。
- 持续计划限制支持 max_executions/total_cost=0 的真实禁止语义；并发、待执行、恢复、材料与 policy 上界与原合同一致。
- 原样保留请求中显式 null 与省略字段的区别，不在适配器里重新采样 now 或重新生成 request_id。
- starts_at:null 的规范化发生在真实 service 中；时区、交易日覆盖和 DST 语义仍由真实 scheduler/calendar 验证，不在 API 用假日历放行。
- 请求最大 256 KiB；JSON 字符串通过 duplicate-key-safe parser；重复键、NaN、非 JSON 正文、额外身份字段均拒绝。

## HTTP

所有路径以 `/personal/routines` 开头：

| 方法与路径 | 真实 service 方法 |
|---|---|
| GET `/context` | context |
| GET 根路径 | list(status/query/limit/after) |
| POST `/previews` | preview |
| POST 根路径 | create |
| GET `/{id}` | get |
| GET `/{id}/history` | history(limit/整数 after) |
| GET `/{id}/occurrences` | occurrences(limit/原 occurrence after) |
| POST `/{id}/revise` | revise |
| POST `/{id}/{pause|resume|cancel|extra-run}` | control |
| GET `/requests/{request_id:path}` | request_status |

HTTP 是认证人工管理通道，适配器明确传 `caller_actor=None`。必须有有效配置的运行时密钥，并使用单个 X-API-Key 或 Authorization Bearer 头；无配置不进入匿名模式。查询参数 api_key、重复凭据头、混合两种凭据头均不通过。

查询白名单逐路由区分：list 仅 status/query/limit/after，history/occurrences 仅 limit/after，其余为空；同名重复参数与数字非十进制整数表示均拒绝。所有成功与接入层错误响应均为 `Cache-Control:no-store`、`Pragma:no-cache`。

错误响应返回稳定 code 和中文说明，不回显异常原文、无效请求正文或 provider 详情。未知写入只指引查询原 request，不宣称未受理或自动重放。

## Agent Tool 与来源约束

Tool 名称为 `personal_routines`；metadata.requires_confirmation=True，持续付费委托不作为免确认工具。

每次调用执行真实 `runtime.require_current_agent()`：普通主对话绑定 `main`，个人 Agent 绑定当前档案 ID，active-turn/session/version 校验沿现有 runtime。模型参数没有 caller_actor、owner、trusted_channel、账户授权或人工已读入口。主 Agent 的范围仍由 service 限制为其发起或本 Agent 的计划，不提升为人工管理身份。

工具操作：context/list/get/history/occurrences/requests/preview/create/revise/pause/resume/cancel/extra-run。写操作形状为：

```json
{"operation":"preview","payload":{"request_id":"稳定编号","definition":{}}}
```

示例中的空 definition 仅展示外层结构；实际 schema 要求完整定义。revise 与控制操作另有 outer routine_id；payload 与 REST 正文保持相同。

内容返回规则：

1. list/history 仅返回 ID、修订、状态、epoch、定义摘要和时间摘要，不返回标题、instructions 或来源正文。
2. preview 仅返回 preview_id、definition_digest、expires_at、近期 slots、base ID/revision；不回传 definition。计划尚不存在时不伪造 provenance pin。
3. get/create/revise/pause/resume/cancel 返回 Routine 正文之前，必须等待真实 `service.pin_plan_read(publicRoutine, actor)`。该 barrier 失败或缺失时不返回正文，不设置测试替身专用通道。
4. occurrences/extra-run 仅返回原发生 ID、原子事项 ID、时间、费用等 opaque 记录，去掉 child、decision 和 body。详细执行与报告仍走原 personal_items/personal_artifacts 权限与来源链。
5. requests 按 result_kind 使用同样规则：Routine 先 pin，preview 去定义，occurrence 去正文。模型读取结果不生成任何人工已读。

## CLI/TUI/Web 手工命令

一个 `parse_routines_command`，一个 `execute_routine_operation`，统一 `/routines`。

```text
/routines context
/routines list [--status active|paused|cancelled|closed] [--query 搜索词] [--limit 数量] [--after 计划ID]
/routines get <计划ID>
/routines history <计划ID> [每页数量] [原修订游标]
/routines occurrences <计划ID> [每页数量] [原执行游标]
/routines requests <原request_id>
/routines preview-json '<完整预览JSON>'
/routines create-json '<完整确认JSON>'
/routines revise-json '<含routine_id的完整修改JSON>'
/routines <pause|resume|cancel|extra-run>-json '<含routine_id的完整控制JSON>'
/routines <pause|resume|cancel|extra-run> <计划ID> <原修订> <稳定request_id>
```

JSON 写入不经 shlex 重写正文，保留 Windows 路径、引号、原稳定编号和精确摘要；普通文本参数支持引号。缺少 request_id 不自动生成，不根据超时、列表空行或未知回执重放。输出保持机器可读 JSON，并转义终端控制字符；错误文案使用中文。

人工 CLI/TUI/Web 调用同一 service dispatcher，caller_actor=None，不套用模型正文摘要；人工身份由可信入口确定，用户 JSON 中没有 trusted/caller 参数。

## 集中验证

接口模块完成后集中执行新 routine 接口与已有 Item/Connection 接入回归、ruff 和真实 import。

首轮：339 passed / 1 failed / 4 errors，exit 1；ruff 4 项行长度，exit 1；真实 import 通过。唯一断言失败是缺少 pin 方法时错误被分类为 invalid_input，已改为显式 configuration_error 并保持正文不返回；四项 setup/teardown error 是两条 256 KiB 极端参数的自动 pytest ID 超过 Windows 32767 字符环境变量上限，已仅设置短用例 ID，保留实际超长输入。帮助和检索提示换行后满足 lint。

最终集中结果：

| 命令 | 字面输出 | 退出状态 |
|---|---|---|
| `uv run pytest tests/unit/test_personal_routine_interfaces.py tests/unit/test_personal_item_interfaces.py tests/unit/test_personal_connection_interfaces.py -q` | `342 passed in 8.64s` | 0 |
| `uv run ruff check src/naumi_agent/personal/routine_tools.py src/naumi_agent/personal/routine_commands.py src/naumi_agent/api/routes/routines.py tests/unit/test_personal_routine_interfaces.py` | `All checks passed!` | 0 |
| `uv run python -c "import naumi_agent.personal.routine_tools, naumi_agent.personal.routine_commands, naumi_agent.api.routes.routines; print('ROUTINE_INTERFACES_IMPORT_OK')"` | `ROUTINE_INTERFACES_IMPORT_OK` | 0 |

首轮和最终原始日志分别为 `.naumi/transactions/pa04-recurring-20261105/interfaces-first-*`、`interfaces-final-*`，失败记录未覆盖。最终源码与文档均逐一重开、计算 SHA-256，清单见同目录 `interfaces-frozen-manifest.json`。

测试使用真实 ASGI/严格序列化/现有实际 runtime 身份校验，业务 service 侧使用显式、带 pin barrier 的 Port。该层只证明适配器和 pin 调用闭环，不替代真实 SQLite/service、真实来源产物、Job/Run、冷重启、浏览器或 provider 验收。主事务负责这些更高层验证；本模块没有启动账户、provider 或浏览器，也没有为 Port 引入生产放行分支。
