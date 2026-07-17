# HAR-07 Completion Receipt UI 与恢复

## 目标

让新 UI 和 TUI fallback 都以同一权威 Harness Receipt 展示完成状态，并可进入 Explain、
Evidence、Check 和 Replay 详情，而不是从模型自然语言猜测结果。

## 子模块

| ID | 子模块 | 用户结果 |
| --- | --- | --- |
| HAR-07.1 | Protocol message | `harness/receipt`, `harness/explain`, `harness/replay` |
| HAR-07.2 | Compact card | 状态、耗时、检查、证据、风险、警告 |
| HAR-07.3 | Detail view | criteria/check/evidence/failure classification 分区 |
| HAR-07.4 | Recovery | resume/reconnect 后按 revision 补发且幂等 |
| HAR-07.5 | Interaction | `e` explain、`r` replay、`v` evidence、复制回执 |
| HAR-07.6 | TUI parity | Textual 表面语义一致，布局可降级 |

## 视觉语义

- verified 使用绿色；unverified 黄色；blocked/失败红色；基础设施问题黄色而非伪装测试失败。
- Git additions/deletions 分别绿色/红色；未跟踪、恢复、警告使用独立语义色。
- 窄屏先保留状态、失败分类和下一步，再裁剪次要 digest/时间。

## 验收标准

- Receipt 先持久化后发事件；丢包用 request/revision 补齐，不产生两张卡。
- 运行完成、部分完成、取消、权限拒绝、Store 故障五种真实场景可区分。
- `/resume` 后 card 与关闭前一致，但瞬态 focus/sidebar 回到默认状态。
- 80/120/200 列和中文宽字符下无溢出，色彩关闭时仍可仅凭文字区分。
- New UI 与 TUI snapshot 的字段集合相同。
- A3：真实 Bridge 进程和新的 Store 实例恢复回执、Explain 与 Replay。

## 非目标

不在前端重新分类失败，不允许 UI 改写 Receipt。

## 实现进展（2026-07-17）

### HAR-07.1a 已实现：类型化 Harness Receipt

- Python Bridge 在收到已持久化的 `harness_completion_receipt` 后，先发
  `harness/receipt`，再保留原有 `ui/message` 兼容消息。
- `harness/receipt` schema v1 严格校验 run id、revision 和三种机械状态；数组数量、公开
  文本长度和前端保留 run 数均有上限，check/criterion 使用字段白名单。
- New UI 按 `run_id + revision` 幂等保存最新类型化回执，但本切片不额外渲染第二张卡，
  因而不会与兼容消息重复。
- 协议契约已同时更新 Python Enum、共享 JSON contract 和 Node normalizer。

### HAR-07.1b 已实现：类型化 Explain/Replay 查询与补发

- New UI 可发送 `harness/explain/request` 或 `harness/replay/request`，以严格校验的
  `run_id + known_revision` 查询当前工作区的持久化运行；Bridge 分别返回
  `harness/explain` 与 `harness/replay`。
- 显式请求总会补发权威 schema v1 / revision 1 响应；New UI 按 `run_id + revision`
  幂等保存 Explain 与 Replay，分别最多保留 100 个 run，同 revision 重传不会覆盖或重复渲染。
- schema v1 只缓存已结束运行的不可变详情；运行中、嵌套 run id 不一致或成功结果残缺时，
  Bridge 返回类型化 `unavailable`，避免固定 revision 1 掩盖后续状态变化。
- Python serializer 与 Node normalizer 双端执行字段白名单、公开文本和集合数量上限；缺失与
  暂不可用状态使用类型化 `lookup_status`，不依赖 Markdown 或模型文案解析。
- 查询复用 `HarnessService.explain_run()` 与 `replay_run()` 的工作区隔离及安全回放语义，
  不触发模型、工具、Harness 检查或 ChatRun。真实 SQLite Store 经新 Service、Bridge JSONL
  到 Node normalizer 的链路已验证，并覆盖跨工作区拒绝。
- 本切片只建立类型化状态，不渲染第二张卡；可见卡片与交互仍由后续 HAR-07 子模块负责。

### HAR-07.2 已实现：单一紧凑完成回执卡片

- New UI 按权威 `run_id` 将 `harness/receipt` 合并到既有 `完成回执`，支持 Harness 先到、
  通用回执先到和后续更高 revision 更新；相同或更旧 revision 幂等忽略，不新增第二张卡片。
- 紧凑区展示 Harness 已验证/未验证/阻塞、检查通过数、准则满足数、去重后的证据引用数、
  至多两项未通过检查和至多两条警告；额外内容用数量提示，避免长回执淹没对话。
- `failed` 使用红色；missing/stale/timeout/cancelled/policy/infrastructure 使用黄色并保留明确
  中文标签，基础设施异常不会被伪装为测试失败；关闭 ANSI 色彩后仍能仅凭文字区分。
- 既有 `Harness 完成回执` 兼容 `ui/message` 已从 New UI Bridge 退役，原始 engine event、
  类型化事件和通用完成回执仍完整保留，因而一次运行只产生一张可见完成卡片。
- 真实 Bridge 事件经过 Node protocol normalizer、reducer 和卡片 renderer 的链路已验证；
  80/120/200 列与中文宽字符均不越界。没有 Harness 同伴的通用回执保持原行为。

### 尚未完成

- HAR-07.3：Harness 详情页。
- HAR-07.4：resume/reconnect 后的权威状态恢复。
- HAR-07.5：`e/r/v` 与复制交互。
- HAR-07.6：新 UI/TUI 字段集合 snapshot parity。
