# Terminal Scoped Permissions Design

> 历史设计说明：2026-07-14 用户将权限语义改为“取消高风险二次确认，bypass 即全权限通过”。本文保留当时的设计背景；当前权威行为以 `docs/product/terminal-ui/03-execution-timeline-and-permissions.md` 和实现/测试为准。

## Goal

将当前“允许一次 / 拒绝 / 切换全局 bypass”的权限确认，升级为可审计、可撤销、不会跨会话残留的授权系统：

- 中风险调用支持“允许一次”或“本会话允许该工具族”。
- 高风险调用必须经过服务端校验的二次确认，任何 bypass 都不能跳过。
- 绝对危险行为保持硬阻断，不提供确认入口。
- 多个并行工具调用可以同时等待权限，响应必须精确关联到原调用。
- 会话结束、Bridge 重启或显式撤销后，临时授权立即释放。

## Existing Problems

当前实现把 `bypass` 同时用作运行模式和权限决定。用户在一次权限卡中选择 bypass 后，`AgentEngine` 会切换全局运行模式，后续路径沙箱、危险命令检查和逐次确认都可能被跳过。这个行为存在四个问题：

1. 授权范围过大：一次工具确认会影响所有后续工具。
2. 生命周期不清：授权不会随单个任务完成自动收敛，只能依赖再次切换模式。
3. 高风险没有强制确认：`PermissionChecker` 在 bypass 下直接清除 `requires_confirmation`。
4. 并发关联脆弱：Bridge 可以保存多个 Future，但终端状态只有一个当前权限卡，容易覆盖并误响应。

## Product Decisions

### Permission Outcomes

权限请求只允许以下决定：

- `allow_once`：只允许当前 `call_id` 对应的一次调用。
- `deny`：拒绝当前调用，不影响同批其他调用。
- `grant_session`：仅对当前会话中的同一工具族创建临时授权，并允许当前调用。
- `confirm`：完成高风险请求的第二阶段确认，只能携带服务端签发的 challenge token。

旧客户端发送的 `allow` 兼容映射为 `allow_once`。旧客户端发送的 `bypass` 仅在中风险请求中兼容映射为 `grant_session`；高风险请求必须返回可操作的 `double_confirmation_required` 错误，不能静默降级或放行。

### Three Safety Classes

权限层区分三类结果，不能只用布尔值表达：

1. **Hard block**：绝对危险、违反沙箱或当前模式禁止的操作。直接拒绝，不展示允许按钮。例如 `rm -rf /`、设备覆写、超出允许目录且策略不允许的路径。
2. **High risk confirmation**：策略允许执行，但可能造成持久或不可逆影响。必须二次确认，不能创建会话授权。例如删除会话、自修改、运行明确标记为 destructive 的工具。
3. **Medium risk confirmation**：策略允许且影响范围可控，但需要用户知情。例如普通 shell、代码执行和后台任务。支持允许一次或本会话工具族授权。

低风险只读或安全操作仍直接执行，不产生权限请求。

### Tool Families

会话授权不按模型返回的原始工具名保存，而按权限规则解析后的 canonical family 保存，避免 namespaced alias 绕过或重复授权：

- `bash_run`、兼容别名和命名空间版本归入 `shell`。
- `code_execute` 归入 `code_execution`。
- `background_run`、后台任务控制归入 `background_process`。
- `runtime_mcp_connect` 归入 `external_runtime`。
- 未显式声明 family 的工具使用解析后的 canonical rule name。

工具族必须由后端权限规则提供，客户端传入的 family 只用于显示，不能参与授权判定。

## Backend Model

### Permission Decision

`PermissionDecision` 扩展为：

- `outcome`: `allow | confirm | block`
- `risk_level`: `low | medium | high`
- `tool_family`: canonical family
- `allow_session_grant`: 是否可创建会话授权
- `requires_double_confirm`: 是否要求二次确认
- `reason` 和稳定 `code`

现有 `allowed` 与 `requires_confirmation` 在迁移期保留为兼容字段，但 Engine 的新逻辑以 `outcome` 为事实源。

`PermissionRule` 增加 `risk_level`、`tool_family` 和 `allow_session_grant`。普通 `requires_confirmation` 规则默认迁移为 medium；工具元数据 `destructive=True` 或规则显式 high 时升级为 high。动态参数命中的危险命令和路径违规不改变规则本身，而是产生 hard block decision。

### Permission Grants

新增内存态 `PermissionGrantStore`，由 Engine/安全层持有而不是 UI 持有。每条授权至少包含：

- `grant_id`
- `session_id`
- `tool_family`
- `created_at`
- `expires_at`，首版为 `null`，表示随会话结束失效
- `source_request_id`

授权匹配必须同时满足 `session_id` 和 `tool_family`。高风险请求、hard block、不同工具族、不同会话都不能命中已有授权。

授权不写入 SQLite，不进入长期记忆，不随 Bridge 或应用进程重启恢复。Engine 切换会话时清除旧会话授权；会话删除、Engine shutdown 和显式 revoke 也必须释放授权。完成单个工具调用不会释放会话授权，因为其明确语义是“本会话”；完成整个 run 只清理 run 级 pending challenge，不清理会话授权。

### Runtime Bypass Compatibility

`AgentRuntimeMode.BYPASS` 继续作为显式运行模式保留，避免破坏现有 CLI 和配置，但权限语义收紧：

- 可以减少中风险逐次确认，作为既有自动化的兼容入口。
- 不能跳过 hard block。
- 不能跳过高风险二次确认。
- 不能跳过路径沙箱或危险命令检查。
- 权限卡中的操作不再切换 runtime mode。

状态栏必须把 runtime bypass 与 session grant 分开显示，避免把局部授权误解为全局模式。

## Confirmation Protocol

### Request

`permission/request` 必须包含：

- `request_id`，默认使用非空 `call_id`，缺失时生成不可冲突的 UUID
- `session_id`
- `run_id`
- `tool_name`
- `tool_family`
- `arguments_summary`，经过脱敏和长度限制，不能直接展示密钥或完整大参数
- `reason`
- `risk_level`
- `choices`
- `scope`: `call | session`
- `expires_at`
- `requires_double_confirm`

中风险 `choices` 为 `allow_once / deny / grant_session`。高风险第一阶段 `choices` 为 `allow_once / deny`。

### High-Risk Challenge

高风险确认必须由 Bridge 执行两阶段握手：

1. 客户端发送 `permission_response(choice=allow_once)`。
2. Bridge 不 resolve Future，而是生成一次性随机 `confirmation_token`，保存请求 ID、会话 ID、调用 ID 和失效时间，并发送 `permission/confirmation_required`。
3. 用户再次确认后，客户端发送 `permission_response(choice=confirm, confirmation_token=...)`。
4. Bridge 验证 token、请求关联、会话关联、未过期且未消费后，才 resolve 为允许。

challenge 默认 30 秒失效，只能使用一次。错误 token、跨请求 token、重复提交和过期 token 都必须拒绝并保留原请求为待处理状态；用户仍可选择 deny。

### Resolution And Grants

`permission/resolved` 返回：

- `request_id`
- `choice`
- `status`: `allowed | denied | granted`
- `grant`，仅 `grant_session` 时提供公开字段

授权创建由 Engine 的 `PermissionGrantStore` 完成。Bridge 负责确认握手和协议关联，不自行决定工具是否属于可授权范围。Engine 必须再次验证 `allow_session_grant`，防止伪造客户端响应。

### Revoke

新增客户端事件 `permission_revoke`：

- `grant_id`：撤销单条授权。
- `scope=all`：撤销当前会话全部临时授权。

服务端返回 `permission/grants_changed`，包含当前会话仍生效的授权列表。`/permissions` 显示待确认队列、有效授权和最近决策；`/permissions revoke <grant-id|all>` 复用同一 Bridge 操作。

## Parallel Request Queue

Bridge 使用 `request_id -> PendingPermission` 映射，每个对象独立保存 Future、原始策略、challenge 和创建时间。不得用全局单一 challenge。

终端状态保存有序 `permissionQueue`：

- 新请求追加，不覆盖当前请求。
- 当前请求 resolve 后自动聚焦下一条。
- 同一 `request_id` 的重放更新原项，不重复追加。
- `permission/resolved` 可以按 ID 移除非当前项。
- run 取消、会话切换、Bridge shutdown 时，所有关联请求统一 resolve 为 deny 并清空 challenge。

并行工具是否等待确认由各自 Future 决定。一个请求被拒绝、超时或取消，不应取消同批其他工具。

## Terminal UX

### Medium Risk

权限卡展示工具、Agent、风险、影响摘要和授权范围：

- `y`：允许一次
- `g`：本会话允许该工具族
- `n`：拒绝

选择 `g` 后卡片显示“已授权本会话 · shell”，状态栏显示临时授权数量。`Shift+Tab` 始终只负责切换 runtime mode，不再充当权限选择快捷键。

### High Risk

第一次按 `y` 后卡片进入明确的确认态，显示高风险原因和“再次按 Enter 确认，Esc 返回，n 拒绝”。只有第二次 Enter 才发送带 token 的 `confirm`。不显示 `g`，也不显示 bypass。

### Queue And Focus

权限卡标题显示 `权限确认 1/3`。上下方向键可查看等待项，但键盘决定只作用于当前聚焦 request ID。新请求到达时不抢走用户正在确认的高风险第二阶段。

## Redaction And Audit

展示参数必须使用统一摘要器：

- key 名匹配 `token`、`secret`、`password`、`authorization`、`cookie` 时替换为 `[已隐藏]`。
- 单字段最多 160 字符，总摘要最多 1200 字符。
- 二进制和不可序列化值使用类型占位符。

每次请求、challenge、允许、拒绝、授权、撤销、超时和取消都写入 permission bubble/history，包含 request/call/session/run ID，但不记录原始敏感参数。审计历史有界，继续沿用当前 100 条上限。

## Failure Handling

- 没有确认入口：工具返回中文错误，提示切换到支持确认的 TUI/CLI；不建议用户切换 bypass 绕过。
- 客户端断开：所有待确认请求按 deny 收敛。
- challenge 超时：回到第一阶段并提示重新确认，不自动拒绝工具。
- Engine 会话变化：旧会话所有 grants 和 pending requests 失效。
- 重复 response：已 resolve 的请求返回 `unknown_permission_request`，不能重复执行工具。
- grant 创建失败：当前调用拒绝，不能退化为 allow once。

## Migration

迁移按以下兼容顺序完成：

1. 安全层先引入风险分类、授权存储和高风险不可 bypass 约束，保留旧 confirmer 返回值。
2. Bridge 扩展新 choices 和高风险 challenge，同时兼容 `allow` 与中风险旧 `bypass`。
3. Terminal 改为队列、`g` 授权和二次确认，更新协议合同与 fake bridge。
4. 旧 Textual/Prompt Toolkit UI 复用同一选择语义；迁移完成前仍能 `allow_once/deny`，但不得放行高风险单次响应。
5. 删除 UI 内“bypass 并执行”的旧文案和映射，但保留 `/mode bypass`。

## Verification

### Safety Unit Tests

- 中风险 shell 返回 confirmation、工具族 `shell` 且允许 session grant。
- destructive 工具返回 high risk、双重确认且禁止 session grant。
- hard-block 命令在 bypass 模式仍被拒绝。
- 高风险在 bypass 模式仍要求确认。
- grant 只命中相同 session 和 family。
- revoke 单条、revoke all、切换会话和 shutdown 都释放授权。

### Bridge And Protocol Tests

- 中风险 allow once、deny、grant session 全链路。
- 高风险第一次 allow 不 resolve，第二次合法 token 才 resolve。
- 错误、跨请求、过期和重复 token 均不能放行。
- 两个并行请求乱序响应时精确 resolve 对应 Future。
- disconnect/cancel 统一 deny 并清理 challenge。
- 旧 allow 映射成功；高风险旧 bypass 被拒绝。

### Terminal Tests

- 权限队列不会覆盖，resolve 后自动前进。
- `y/g/n` 只作用于聚焦请求。
- 高风险二次确认期间新请求不抢焦点。
- 高风险卡不出现 grant/bypass。
- Shift+Tab 不再发送 permission response。
- `/permissions` 可查看和撤销真实后端 grants。

### Real End-to-End Scenarios

1. 运行两个并行的中风险工具，分别授权和拒绝，验证结果不串线。
2. 对 `shell` 创建 session grant，再运行普通 shell，验证不再提示；运行 code execution 仍提示。
3. 触发可执行的 high-risk 工具，验证一次允许不执行、二次确认后才执行。
4. 在 runtime bypass 下触发 hard block 和 high risk，分别验证硬阻断和二次确认。
5. 撤销 grant 后再次调用同族工具，验证重新出现权限请求。
6. 结束会话并新建会话，验证旧 grant 不再生效。

## Non-Goals

- 本阶段不持久化跨会话或跨设备权限。
- 本阶段不提供按参数模式授权，例如“只允许某个目录下的 shell”。数据模型保留未来增加 constraints 的空间。
- 本阶段不让 LLM 自主批准自己的权限；所有 confirmation 和 grant 必须来自受信任的人类客户端。
- 本阶段不取消 runtime bypass 模式，只收紧它对 hard block 和 high risk 的影响。
