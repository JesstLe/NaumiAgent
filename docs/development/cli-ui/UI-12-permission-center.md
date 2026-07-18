# UI-12 权限策略中心

## 目标

把待确认请求、最近决定、规则来源、风险和 mode/bypass 范围做成可解释策略中心。

## 子模块

- UI-12.1 Policy snapshot：工具 metadata、prefix rule、workspace、mode、来源优先级。
- UI-12.2 Pending queue：一次一个活动确认，其他请求排队并显示等待顺序。
- UI-12.3 Decision history：allow/deny/bypass、actor、scope、时间、关联 call/run。
- UI-12.4 Rule explanation：命中哪条规则、为何需确认、bypass 覆盖与不覆盖边界。
- UI-12.5 Scoped grant：once/session/workspace；持久授权必须明确用户选择。
- UI-12.6 Recovery：前端断线后重新获取 pending，已决定 request 不重复确认。

## 验收标准

- bypass 对常规工具全通过，但系统不可破坏边界、资源限额和审计继续生效。
- 并发 20 个确认不串 call id；超时/取消/重复 response 幂等。
- reason 可显示但持久 Evidence 不保存原始私密原因。
- plan/default/bypass 状态在 Footer、中心页和 Python 权限层一致。
- 新 UI、TUI、无交互模式分别有明确确认或拒绝策略。
- 权限拒绝能被 Harness Explain 分类，用户有下一步。

## 实现进展（2026-07-19）

### UI-12.1a 已实现：类型化策略快照与只读中心页

- Bridge 的 `/permissions` 不再把 ANSI/Markdown 字符串塞入通用消息，而是发送严格白名单的
  `permissions/snapshot` schema v1；pending、grant、history 和 warning 均有数量与文本上限。
- 新 UI 使用瞬态全屏路由展示运行/权限模式、待确认、有效授权、最近决定和规则来源；支持刷新、
  键盘滚动与 Esc 恢复对话锚点，显式 resume 不保留旧会话页面状态。
- bypass 明确显示为常规工具全权限放行；规则风险与 default/plan 下的确认要求仍可解释。
- TUI fallback 继续使用同一 Python snapshot builder 的共享文本 renderer，没有复制策略查询逻辑。
- 历史/授权读取异常只输出固定脱敏警告，不再把异常类型或正文暴露给 UI。
- 真实 AgentEngine、会话 grant、pending Future、Bridge 和 Node 页面在 80/120/200 列通过验证。
- 详细协议与非目标见 `UI-12-1a-policy-snapshot-design.md`。

### UI-12.3a 已实现：用户终态决定持久回执

- allow once、session grant、bypass 与 deny 形成独立 SQLite 回执，绑定 actor/source/session/run/call/tool 和
  参数摘要；不持久化原始参数与私密 reason。
- 新 UI/TUI 的共享 Permission Panel 优先读取当前 session 的 durable history，并显示操作者、来源与时间。
- execution grant 必须消费匹配的真实回执引用，任意字符串、denied 或绑定不一致均 fail closed。
- 详细合同、验收和剩余边界见 `UI-12-3a-durable-decision-receipts.md`。

### 尚未完成

- UI-12.2：可操作的 pending queue 与等待顺序。
- UI-12.3b：补齐 policy/Hook/plan block taxonomy、跨会话查询与 retention/export policy。
- UI-12.4：针对单次检查结果的完整规则解释链。
- UI-12.5：workspace scope 持久授权。
- UI-12.6：断线后的 pending 恢复和已决定 request 幂等。
