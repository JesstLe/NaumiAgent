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
