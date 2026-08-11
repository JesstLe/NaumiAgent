# EVO-06.1c3 Durable Exact Tool Catalog Miss Opportunity

## 状态

Implemented。本切片把 `tool_search` 对 `select:<tool-name>` 的精确缺失结果保存为工作区隔离、
内容摘要可校验的 durable fact，并投影为动态可撤权的 `tool_catalog_miss` capability Candidate。
自然语言搜索 miss 不进入 Store，不把模型猜测或低相关匹配冒充缺失能力。

## 为什么只接受精确 select miss

关键词搜索同时包含用户自然语言、同义词、排名阈值和当前 metadata，零结果不等于能力不存在。直接保存
原查询会泄露会话意图，仅保存 query hash 又无法重放其语义。因此本切片限定：

- 输入必须由现有 `tool_search` 的 `select:<tool-name>` 路径产生；
- 工具标识符必须是字母开头、1..128 位，仅含字母、数字、`_ . : -`；
- Store 只保存安全规范化工具名，不保存完整 query、用户消息或模型推理；
- 空关键词、普通关键词零结果、包含空格/控制字符的 select 值均不产生 miss ID。

关键词缺失意图的安全结构化仍是后续独立模块，不能由本切片虚报完成。

## Durable Miss Store

`ToolCatalogMissStore` 由 Runtime Composition Root 构造，与 Candidate Store 共用用户所有的
`.naumi/evolution.db`，但使用独立表和资源契约。每条记录包含：

- content-addressed `tsm_<24 hex>`；
- canonical workspace root（仅保存在 source Store，不进入 Candidate）；
- normalized requested tool name；
- 排除 `tool_search` 自身后的完整注册工具名目录 SHA-256；
- 首次观察时间、canonical JSON 和 record SHA-256。

主键与唯一约束为 workspace + miss ID / requested name + catalog digest。8 个并发相同搜索收敛为同一
不可变记录，不累计次数，避免 Agent 通过重复搜索人为提高机会优先级。SQLite 使用 `BEGIN IMMEDIATE`、
5 秒 busy timeout、0700 目录和 0600 数据库权限。

读取时同时校验 JSON 摘要、投影列、content-addressed ID 和 workspace。跨工作区读取返回不存在；字段或
摘要被修改时抛出 corruption，不允许降级为有效事实。

## 动态 authority

发现与每次 Review gate 都执行以下重验：

1. miss ID 必须在当前工作区的 durable Store 中存在且完整；
2. requested tool 仍不能被 Tool Registry 的精确或规范化名称解析；
3. 当前完整工具名目录摘要必须与观察时一致；
4. 目录新增/删除任意工具后，旧 miss 失败关闭并要求重新搜索；
5. 如果目标工具已经出现，返回 `tool_catalog_miss_satisfied` 并撤销既有 Candidate authority；
6. Store 不可用、篡改或跨工作区时返回 source unavailable。

目录摘要绑定防止“工具目录先改变、旧 miss 后自动复活”。如果工具后来再次移除，必须产生新的当前
catalog miss，不能复用历史 authority。

## Evidence 与 Candidate

| 字段 | 值 |
| --- | --- |
| source kind | `tool_catalog_miss` |
| source URI | `tool-search://misses/<miss-id>` |
| finding | `missing_tool_capability` |
| scope | `capability:tool:<requested-name>` |
| Candidate kind | `capability` |
| expected metric | `tool.catalog.requested_capability.availability` |
| verifier | `tool_catalog_presence` |

root fingerprint 只绑定 finding、requested name 与 scope；同一能力在新目录中仍缺失时，新 Evidence 会
并入同一 Candidate revision，而不会产生两个逻辑能力候选。Candidate 不包含 workspace、自然语言查询、
会话正文或 Tool Search 输出。

`tool_catalog_presence` 已进入 typed Proposal/Plan/Cohort contract，但 Metric Runner Registry 当前明确返回
`tool_catalog_acceptance_runner_unavailable`。仅注册同名工具不足以证明能力可用；在独立 runner 能验证
schema、权限、真实调用链和用户验收前，不签发自动 Experiment Contract。

## 双通道与用户体验

1. Agent 或用户调用 `tool_search(query="select:browser_trace_compare")`；
2. 搜索回执显示 `tsm_...` 和下一步命令，不隐藏持久化行为；
3. 用户 Slash：`/evolution discover-miss <miss-id>`；
4. Agent Tool：`evolution_discover_tool_catalog_miss_opportunity`；
5. 两个入口共享 `EvolutionToolCatalogMissOpportunityService.discover()`；
6. Review filter 支持 `tool_catalog_miss`，New UI 与 Textual TUI 消费同一个 typed projection；
7. normal/bypass 无二次确认，lockdown 禁止，单会话上限 50。

如果 miss Store 写入失败，Tool Search 仍返回搜索结果，但明确提示不能进入 Evolution；不会伪造 tsm ID。

## 分层与兼容

- Store 由 `RuntimeResources` 显式拥有，可注入和测试，不由 Tool 隐式创建；
- Evolution service 只依赖 Tool Catalog 的 names 协议，不导入 Agent Engine；
- `tools.ToolSearchTool` 的兼容导出改为惰性加载，避免 `candidate → safety → tools → search → evolution`
  导入环，同时保留原公共名称；
- 旧 `ToolSearchTool(registry)` 仍可用于无持久化的独立场景；生产 composition 必须同时传入 Store 和
  workspace，禁止只提供其中一个。

## 验收标准

- [x] 8 个并发 exact miss 搜索收敛为一个 tsm，8 个并发 discovery 收敛为 Candidate revision 1；
- [x] 自然语言和不安全 select 查询不创建数据库、不写入 Evidence；
- [x] 目标工具出现、无关目录变化、篡改和跨 workspace 均撤销或拒绝 authority；
- [x] Candidate 是 capability/tool Proposal，且固定不可执行、不可自动实验；
- [x] Metric binding 对缺少 acceptance runner 明确 blocked；
- [x] 真实 Engine `_execute_tool` 完成 Tool Search → durable miss → Evolution Candidate 链；
- [x] Runtime Composition、Source Router、权限、Slash、Agent Tool、New UI/TUI filter 使用同一实例；
- [x] Ruff、文档治理和相关小模块测试通过；按用户要求不运行全量测试。

## 自我审视与未完成项

本切片只证明“当前精确工具目录中不存在该工具标识符”，不证明用户真正需要该能力，也不证明未来同名
实现满足语义。它必须与 durable Goal、反馈或长期 Outcome 在后续聚类/优先级阶段组合，不能单独自动造工具。

下一步是 EVO-06.1c4：跨 `rollback_outcome`、`promoted_outcome`、`eval_metric_regression`、`goal_need` 和
`tool_catalog_miss` 的有界时间窗聚类与可解释优先级；随后才进入 EVO-06.2 Capability Proposal。
