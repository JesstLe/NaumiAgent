# UI-13.1a Typed 本地 Health 状态页

## 1. 目标

把现有 `/doctor` 的真实本地检查从单段 Markdown 升级为新 UI 可验证、可滚动、可刷新的 typed Health
状态页，同时保留 Markdown 系统消息作为旧客户端和诊断页失败时的文本 fallback。本切片不运行付费模型
探测，不自动联网修复，也不提前实现 Trace viewer 或诊断包导出。

## 2. 权威模型

`DoctorHealthSnapshot` 从同一次 `DoctorReport` 派生，包含：

- `status`：`ok/degraded/error/unknown`；
- `items`：由 domain 与检查名称摘要生成的稳定唯一 ID，以及 label、severity、responsibility、detail、
  suggestion；插入新检查不会改变已有 ID；
- domain：runtime/model/provider/store/git/node/browser/mcp/terminal；
- responsibility：user_config/local_environment/external_service/product_runtime/unknown；
- `snapshot_sha256`：不含生成时间的 canonical 内容摘要，重复事实产生相同摘要；
- `live_probe=false`：明确说明本次只运行本地检查。

公开文本限制为 500 字符、最多 64 项，不携带配置对象、异常对象、secret、环境变量全集或原始日志。
API Key 检查只报告是否存在和配置入口，不显示凭据内容。

## 3. 真实检查与降级

复用 `run_doctor()` 已有的 Python、配置、provider、模型能力、搜索、workspace、Store Catalog、Git、
ripgrep、Docker、browser daemon、MCP、debug log 和 terminal 检查，并新增真实 `node --version` 本地检查。
打开页面不会调用 `_check_live_model()`；未来 live probe 必须由用户显式触发并继续遵守预算/取消约束。

若 Doctor 自身抛出异常，Bridge 记录异常类型到 DebugTrace，并返回一个 `product_runtime/error` typed item 和
`/debug` 下一步；不会把底层异常正文、路径或 secret 送入 UI，也不会让页面永久停在 loading。

## 4. 新 UI

- `/doctor` 打开 transient 全屏页，`r` 重新运行同一组本地检查，`Esc` 恢复原会话滚动锚点；
- 页面把前端真实 Bridge heartbeat 作为 runtime item 合并展示：healthy/stale/starting 分别映射
  ok/error/unknown；
- 每项以文字同时表达严重度、domain、归因、详情和下一步，颜色只作辅助；
- 80/120/200 列不溢出，长报告支持方向键、PageUp/PageDown、Home/End。

## 5. 验收标准

- Python 与 Node 对 `doctor/health` 事件清单、字段、枚举和边界得出相同结论；
- Node 存在时显示真实版本，不存在时显示本机环境受限及安装建议；
- 缺 API Key 明确归因用户配置，不触发 Keychain 或 live provider 请求；
- Store、browser/MCP、terminal 与 provider 不混为同一故障域；
- stale heartbeat 明确提醒不要重复提交，首次心跳前显示未知而非伪造正常；
- Doctor 自身失败返回脱敏 product runtime fallback；
- typed 页面与兼容 Markdown fallback 同时存在；相关小模块测试与真实本地 Doctor 场景通过。

## 6. 后续

- UI-13.1b/13.2：补齐 provider HTTP 401/404/429/5xx 稳定 code 与聚合摘要；
- UI-13.3：用户显式启动、可取消、有预算的 live probes；
- UI-13.4：基于 DebugTrace 的 typed trace viewer；
- ARC-08：把相同 Health contract 接入 SLO、故障审计和恢复建议；
- CC-03：诊断组件对齐时复用该 contract，而不是解析 Markdown。

HAR-10.2b 已把当前 Goal 的 Pursuit Recovery Snapshot 作为独立 runtime health item 接入本页，并参与整体
severity；现有 Bridge heartbeat 仍只代表前端连接活性，与 worker heartbeat 分栏呈现。没有当前 Pursuit 时
不会制造虚假 recovery item。
