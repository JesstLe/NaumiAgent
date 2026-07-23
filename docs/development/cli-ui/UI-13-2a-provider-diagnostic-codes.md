# UI-13.2a 稳定 Provider 诊断码

## 问题

Doctor 已能把常见 Provider 失败翻译为中文，但调用方只能解析自然语言。相同的认证、模型地址、限流或
服务端故障无法被 New UI、TUI、CLI、后续 Trace/Runbook 稳定关联；原始异常正文又可能包含凭据、请求
内容或供应商私有信息，不能直接作为机器合同。

本切片在既有 Doctor 权威上增加脱敏、低基数、可跨界面消费的诊断码，不新增第二套 Provider 探测器。

## 合同

- `DoctorCheck.diagnostic_code` 是可选、最长 64 字符的小写 snake_case；普通健康检查保持空值。
- 本地配置错误产生稳定码：
  - `provider_credentials_missing`；
  - `provider_model_missing`；
  - `provider_config_invalid`；
  - `provider_temperature_invalid`；
  - `provider_prerequisite_failed`。
- 用户显式执行 `naumi doctor --live` 时，实时错误映射为：
  - 401/403 → `provider_auth_failed`；
  - 404 → `provider_resource_not_found`；
  - 429 → `provider_rate_limited`；
  - 5xx → `provider_server_error`；
  - timeout → `provider_timeout`；
  - 网络连接错误 → `provider_connection_failed`；
  - 其他失败 → `provider_request_failed`。
- 优先读取异常或 response 的结构化 `status_code`；只有不存在结构化状态时才从异常类别/文本提取有界
  状态证据，避免错误正文中的其他数字覆盖真实 HTTP 状态。
- 用户可见 detail 不包含原始异常正文；诊断码不包含 provider、model、URL、用户 ID 或 request ID，避免
  secret 泄漏和高基数标签。
- `DoctorHealthItem` 以向后兼容的 schema v1 可选字段携带 `diagnostic_code`。Python 会把内部非法码降级为
  `diagnostic_code_invalid`、归因产品运行时并提升整体 Health 为 error；Node 边界拒绝外部非法码。

## 界面

- CLI/TUI Markdown 报告在存在诊断码时单独显示“诊断码”，不依赖颜色表达。
- New UI Doctor 页从 typed payload 展示相同诊断码，保留 domain、归因、中文详情与下一步。
- New UI 默认 `/doctor` 仍只做本地检查，不偷偷联网；因此当前可直接看到本地配置码。实时码由现有
  `naumi doctor --live` 验证，后续 UI-13.3 再提供显式、可取消的页面动作。

## 验收标准

- 缺失凭据、Provider/模型不一致、非法 temperature 和前置失败均返回稳定本地码。
- 401、404、429、503、timeout 至少各有定向测试，且 detail 不回显 secret-shaped 原文。
- 当异常正文提到 401、结构化状态为 429 时，权威结果必须是 rate limited。
- Python Health snapshot、Node protocol normalizer、New UI 页面和 Markdown fallback 对同一 code 语义一致。
- 非法 code 不穿过边界；snapshot digest 包含 code，诊断事实变化会改变摘要。
- 本地真实 `/doctor` 场景无需网络即可产生并渲染 Provider 配置码。

## 明确保留边界

- UI-13.3 的显式 live probe 页面、取消、超时与预算 UI 尚未实现；本切片不改变 `/doctor` 默认只读行为。
- 尚未实现 provider 请求级 trace、聚合计数、SLO、自动重试或修复动作；ARC-08 后续只能消费这些低基数
  code，不能从这里推断可用率。
- 不把异常消息、response body 或完整 URL 加入 code/detail；需要深入排障时仍应查看脱敏 DebugTrace。
