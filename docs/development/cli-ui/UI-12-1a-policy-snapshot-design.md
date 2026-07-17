# UI-12.1a 类型化权限策略快照与只读中心页

## 用户结果

新 UI 输入 `/permissions [1..50]` 后进入独立全屏权限策略中心，用户可以直接看到当前运行模式、
权限模式、待确认队列、本会话授权、最近决定以及每个工具命中的规则来源。页面不执行工具、
不修改授权，也不根据前端猜测策略。

Textual TUI 与 deprecated CLI 的 `/permissions` 保留共享 Python renderer；它们和新 UI 都从
`build_permission_panel_snapshot()` 读取同一份 Python 权限事实。

## 类型协议

Bridge 响应 `permissions_panel` 请求时发送 `permissions/snapshot` schema v1：

- `runtime_mode`：`default|plan|bypass`。
- `permission_mode`：`bypass|permissive|moderate|strict|lockdown`。
- `pending`：最多 50 项待确认请求。
- `grants`：最多 50 项当前会话授权。
- `history`：最多 50 项最近决定。
- `warnings`：最多 20 条固定、脱敏的可行动警告。

请求和历史项只允许输出 request/call/session/run/agent/tool、参数摘要、状态、原因、风险、选择、
scope、有效期和规则元数据。每个文本字段最多 500 字符；原始参数、私有 payload、异常正文、
secret 和用户完整对话不进入快照。

## 状态与交互

1. `/permissions` 保存当前对话滚动锚点，打开 `permissions` 瞬态路由并请求快照。
2. `r` 重新读取 Python 权限层；方向键、PageUp/PageDown、Home/End 只改变本地滚动。
3. `Esc` 恢复原对话锚点；显式会话恢复关闭页面并清除旧会话快照。
4. `/permissions revoke <grant-id|all>` 继续走既有后端撤销协议，不由页面直接修改状态。
5. pending、grant 和 history 状态均由后端确认；前端不做乐观审批或伪造终态。

## bypass 语义

- 页面明确显示 bypass 对常规工具为全权限放行，不再出现高风险二次确认。
- 审计记录、显式预算/资源限额和工具实现自身错误不属于权限确认，仍然有效。
- 规则来源、风险与确认要求仍可查看，便于用户理解切回 default/plan 后的行为。

## 视觉与降级

- 待确认使用黄色；允许、授权和已确认使用绿色；拒绝和阻断使用红色。
- 无 ANSI 时保留完整状态文字，颜色不是唯一信息通道。
- loading 与 unavailable 明确区分；没有快照时不能显示“暂无待确认”来伪装成功。
- 80/120/200 列按中文显示宽度折行，页面高度严格受视口约束。

## 验收证据

- Python 单元测试验证规则解析、字段白名单、50/500 上限及异常脱敏。
- Node 协议测试验证 schema、枚举、数量上限和私有字段丢弃。
- 页面与路由测试覆盖三种宽度、加载/失败、滚动锚点和会话瞬态状态。
- 真实 AgentEngine 会话、真实 PermissionGrantStore、真实待确认 Future 经 JSONL Bridge、Node
  normalizer/reducer 和 renderer 的链路通过。

## 非目标

- UI-12.2 的活动确认队列操作界面。
- UI-12.3 的持久审计 Store 和跨会话历史。
- UI-12.5 的 workspace 持久授权。
- UI-12.6 的断线 pending 重取与决定幂等恢复。
