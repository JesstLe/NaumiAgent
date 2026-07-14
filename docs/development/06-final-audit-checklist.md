# 最终审核清单

本清单供最终审核者（当前约定由 Codex 主审核）使用。逐项记录 `pass/fail/not_applicable` 和证据。

## A. 范围与来源

- [ ] commit/PR 只对应一个模块 ID，依赖均已 implemented。
- [ ] 目标文件与模块文档一致；无用户未授权的大规模重构。
- [ ] Claude Code 复用有 source commit/path/license/provenance。
- [ ] 无空壳模块、空表、未接线 Tool 或只为未来准备的死代码。

## B. 接口与状态

- [ ] Schema/类型严格，未知字段、版本和状态有明确行为。
- [ ] 终态幂等；重复、乱序、断线、恢复不会产生第二次副作用。
- [ ] UI/Agent Tool 共用 service；前端不复制权威业务规则。
- [ ] Store/Protocol migration 有兼容和恢复证据。

## C. 正确性证据

- [ ] 已观察与功能缺失直接相关的 RED。
- [ ] 定向 GREEN 命令是本次运行的新鲜输出。
- [ ] lint/type/compile 通过；未运行项目写明原因。
- [ ] 真实场景不是 mock-only，覆盖用户触发到回执。
- [ ] 错误、空输入、极端参数、并发、取消、超时、恢复已覆盖。

## D. 安全与隐私

- [ ] bypass 未绕过审计、资源、签名、protected scope 和系统不可破坏边界。
- [ ] secret/raw stdout/reasoning/认证 header 不进入持久 Evidence、UI state 或导出包。
- [ ] workspace/session/agent/run/job 数据隔离，已知 id 不能跨域探测。
- [ ] 路径、symlink、命令 argv、插件/扩展和 daemon grant 已审查。

## E. 用户体验

- [ ] 状态、进度、取消、等待用户、错误、恢复、完成回执可见。
- [ ] 中文错误包含发生了什么、为什么、下一步。
- [ ] 80 列/无颜色/非 TTY/fallback 仍可操作；动画可关闭。
- [ ] Git、代码、数学、普通文本和状态使用语义色但不只依赖颜色。

## F. 性能与可靠性

- [ ] queue/cache/history/artifact 有界；无明显 O(n²) 热路径。
- [ ] heartbeat、lease、backpressure、retry budget 和 circuit breaker 适用时存在。
- [ ] 故障不会丢失主任务结果；未知副作用不自动重放。
- [ ] 性能/SLO 模块提供 baseline 与差异报告。

## G. 交付结论

- [ ] 文档状态、注册表、测试和代码一致。
- [ ] commit 已推送目标分支，工作树无本模块遗留。
- [ ] 已知不足诚实列出，并指向精确后续模块 ID。
- [ ] 审核结论使用 approved/changes_required/blocked/rejected 之一。
