# Personal Agent 工具权限接缝

日期：2026-11-05

## 问题与本次实现

真实隔离 Engine 的 276 个已注册工具中，有 9 个非只读工具没有精确规则；
在 PERMISSIVE / MODERATE / STRICT 下落入 `UNKNOWN_TOOL`。
本次不合入其他分支的会话、浏览器或进程改动，只补齐当前工具合同。

| 工具 | 风险 / 确认 | 会话额度 | family |
|---|---|---|---|
| `agent_result_acknowledge` | LOW / 无需 | 不新增额度 | `agent_result_inbox` |
| `output_asset_retention_run` | HIGH / 每次确认 | 50 | `output_asset_retention` |
| 五个 `evolution_capability_{specification,governance,artifact,scenario_binding,sandbox_request}` | MEDIUM / 无需 | 各 50 | 各工具同名 family |
| `personal_connections` 的 context/list/events/sync | MEDIUM / 无需 | 200，共享该工具计数 | `personal_connections` |
| `personal_connections` 的 event_to_item | MEDIUM / 每次确认 | 与读取共享 200 | `personal_connections` |
| `personal_actions` | MEDIUM / 无需 | 200 | `personal_actions` |

上述规则允许 BYPASS / PERMISSIVE / MODERATE / STRICT，仍阻断 LOCKDOWN。
全部不授予 session grant；BYPASS 延续原语义，不新增隐式确认。
原通用路径沙箱、工具 schema、调用额度和委派限制保持有效；未注册的非只读工具仍拒绝。

## 为什么日程转事项单独确认

`event_to_item` 通过真实 `personal_items.create` 创建 once/now 事项，可能唤醒后续执行。
它没有另一道独立的人工批准，因此必须与直接创建事项一样请求确认。
读取、同步已授权镜像则不应反复打断用户。

`PermissionChecker` 先解析规范工具名，再派生本次不可变 rule；
仅 `event_to_item` 设置 `requires_confirmation=True`，不修改全局规则。
点号/双下划线命名空间保持同等确认；动态 MCP 继续沿用原 HIGH 规则，不被降级。
未知、畸形、重复 operation 仍由严格解析与 schema 拒绝，不能进入 dispatcher 的转事项分支。

## 外部行动不由普通工具确认批准

`personal_actions.prepare_action` 只提出 `draft`，不产生外部写入权限。
模型接口不含 OAuth、账户授权、人工批准或直接执行操作。
真正批准仍要求可信人工通道与确切 revision/digest；原 actor、来源撤权、费用、原请求核对均保留。
结果已读工具仍只确认当前会话的确切 delivery/hash，不删除加密结果。
输出清理仍是有界 HIGH 操作，不允许会话范围的一次性放行。

## 验证与当前边界

`tests/unit/test_personal_permission_contracts.py` 使用 9 个真实 Tool 实例及真实 Engine：
模式、风险、确认、family、调用额度、重置、命名空间、沙箱、未知工具、模型行政操作拒绝，
以及正常模式真实读取空连接/草稿列表和转事项人工拒绝链路。
账户生命周期、草稿/批准与原请求核对继续由原领域测试集中回归。

本模块不请求模型或真实账户，不重放旧执行，不修改配置，不启动浏览器或部署。
本地真实工具/SQLite/HTTP 证据不替代真实账户和全产品验收。
Windows 创建 symlink 需要系统权限的原测试单独保留为已知环境缺项，不冒充绿色。
未补入整个 core-audit 分支；此前记录的其他缺口和全部 24 项能力继续保留。

原始哈希、完整命令和字面输出、源码副本回滚与后改零写入保护：
`.naumi/transactions/pa-permissions-20261105/VERIFICATION.txt`。
