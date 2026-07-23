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
| HAR-07.4a | Resume Recovery | 显式 resume 从持久化 Store 恢复单一权威卡片 |
| HAR-07.4b1 | Idle Bridge Reconnect | 空闲断线有界重启、重新协商并精确恢复 session |
| HAR-07.4b | Reconnect Recovery | 断线重连按 revision/gap 补发且幂等 |
| HAR-07.5 | Interaction | `e` explain、`r` replay、`v` evidence、复制回执、完成卡直达详情 |
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

## 实现进展（2026-07-23）

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

### HAR-07.4a 已实现：显式 Resume 权威回执恢复

- `HarnessStore.list_session_runs()` 以规范化工作区和精确 session id 为联合边界，按更新时间
  倒序、有界查询持久化运行；不存在的数据库返回空集，非法 limit 与损坏行不会被静默吞掉。
- Bridge 在 `session/replayed` 与历史消息之后，先按时间正序发已完成的 `harness/receipt`，再发
  同 run 的通用 `completion/receipt`。前端因此先填充不可见 Harness 缓存，随后只创建一张完整
  卡片，不会先显示降级状态再闪变；运行中且尚无 receipt 的记录不会伪装成完成。
- Harness Store 恢复失败不会阻断历史消息或通用完成回执，用户会收到固定、脱敏、可行动的
  `harness_receipt_recovery_failed` 提示；内部异常详情不会进入 UI 事件。
- `clear: true` 的替换式恢复会清空 Receipt、Explain 和 Replay 三类 Harness 缓存，避免旧会话
  状态污染；`clear: false` 的追加式恢复保留缓存并继续使用 revision 幂等语义。
- 验收链路使用真实 Session、ChatRun 与 Harness SQLite，关闭写入实例后以全新的 Store、Service
  和 Bridge 恢复，再经过真实 Node normalizer、reducer 与 renderer；验证工作区隔离、事件顺序、
  单卡片，以及 80/120/200 列布局。

### HAR-07.3 已实现：Harness 运行详情视图

- 新 UI 的 `/harness detail [run-id|latest]` 打开瞬态全屏路由，并发请求精确 run id 的类型化
  Explain 与 Replay；Esc 恢复原对话滚动锚点，会话恢复不会保留详情页瞬态状态。
- 页面展示概览、准则、失败分类、检查、证据、Replay、差异和 Artifact 分区；各分区直接消费
  revision 缓存中的白名单字段，不在前端重新推断运行结论。
- completion criterion description 已进入 Explain 类型协议，双端限制条数、文本长度和 evidence
  引用数量；旧 schema v1 响应缺少该可选字段时仍可安全降级为空准则列表。
- TUI fallback 使用同一 `/harness detail` 命令和同一公开字段集合；Explain 成功后固定精确 run id
  再 Replay，避免两次 `latest` 查询指向不同运行。
- 绿色、黄色、红色分别表达成功、未验证/变化、失败/摘要不一致；无 ANSI 时状态文字仍完整。
  真实 Store→Bridge→Node→renderer 链路和 80/120/200 列中文宽字符边界已验证。
- 详细边界与验收见 `HAR-07-3-detail-view-design.md`。

### HAR-07.6 已实现：New UI / TUI 完整字段一致性

- TUI 现在按 `run_id` 合并先到的权威 Harness Receipt 与通用 completion receipt，一次运行只
  产生一张可见完成卡片；没有 Harness 同伴时仍保持原有通用回执行为。
- 两端详情投影都展示协议允许的完整有界字段集合，包括 criterion id/description、finding
  source/next step、evidence digest/URI、具体 anomaly、全部有界 difference 与 artifact；视口
  继续有界并通过滚动访问后续内容。
- 两端共用 `terminal-parity-golden.json`，New UI 样例必须先经过真实 protocol normalizer；
  Textual 测试通过真实 app 与 engine sink 验证单卡片合并，而非只比较独立格式化函数。
- 字段、降级、验收证据和诚实边界见 `HAR-07-6-terminal-parity.md`。

### HAR-07.5a 已实现：详情 Explain / Replay 独立刷新

- New UI 详情页支持 `e/E` 单独刷新 Explain、`r/R` 单独刷新 Replay；首屏显示键位，刷新保留
  滚动位置和另一分区的权威内容。
- 每个分区只允许一个 in-flight 请求，重复按键幂等吸收；请求携带精确 run id 与最后接受的
  revision，相同 revision 仍由 Bridge 重新读取 Store 并补发结果。
- 刷新只读，不执行模型、Harness check、工具或原任务。TUI fallback 继续通过相同
  `/harness detail <run-id>` 命令和共享后端查询刷新两类详情。
- Node 键盘状态机到真实 SQLite Store、Python Bridge、Node normalizer/reducer/renderer 的链路已验证；
  详细边界见 `HAR-07-5a-detail-refresh-interactions.md`。

### HAR-07.5b 已实现：Evidence 焦点与跨表面入口

- New UI 支持 `/harness evidence [run-id|latest]` 和详情页 `v` 切换，只消费已有类型化 Explain，按 Evidence ID
  展示关联准则、失败发现、孤立记录和缺失引用，不重新推断失败或验证结论。
- 完整详情与 Evidence 焦点分别保存滚动位置；Evidence 入口不提前请求 Replay，首次返回全部详情时才按需补发。
- CLI/Textual TUI 通过共享 `/harness evidence` 和相同 Harness Service 展示同一公开字段集合；not found 与
  unavailable 不会被伪装为成功。
- Python/Node 共用 HAR-07 golden fixture，并以真实 SQLite Store → 重建 Service → slash router 验证；详细边界见
  `HAR-07-5b-evidence-focus.md`。

### HAR-07.4b1 已实现：空闲 Bridge 进程重连

- New UI 在无活动运行、permission、interaction 和未确认发送时，可在同一前端进程中有界重启
  Python Bridge；三次尝试都重新 hello 协商、重置 sequence guard，且不会继承旧 Bridge 的
  protocol registry 证明。
- 断线前存在精确 session id 时，新进程 hello 成功后先发送替换式 resume，再释放断线期间的
  deferred sends。HAR-07.4a 随后从持久 Store 恢复 Harness 与通用回执，前端仍只生成一张卡。
- 活动运行或未裁决输入断线继续 fail-closed，并以非零码交给 TUI fallback；不会自动重放
  submit、tool、permission 或 interaction。
- 真实 Node UI + 两个先后启动的 JSONL Bridge 进程已验证重连、seq 重新起点、精确 session、
  单回执和活动运行拒绝。详细状态机与剩余边界见
  `HAR-07-4b1-idle-bridge-reconnect.md`。

### HAR-07.5c1 已实现：完成回执跨平台复制

- New UI、Textual TUI 与 deprecated CLI 共享 `/copy receipt [receipt-id|latest]`；查询始终绑定当前
  session，New UI 不再依赖缺失的 frontend adapter。
- `ChatRunStore` 提供通用回执 authority；同 run 的 Harness Receipt 只有在 workspace/session 双重一致时
  合并，Harness Store 故障会明确降级为通用回执。
- 完成卡显示精确复制命令。导出只使用共享有界卡片投影，不包含 reasoning、原始工具输出、
  Evidence URI 或变更绝对路径；文件唯一保存到 `.naumi/exports`，剪贴板不可用时仍返回路径。
- macOS/Windows/Linux 分别使用 `pbcopy`、`clip`、`wl-copy|xclip`，后端有 3 秒超时；文件名前缀白名单、
  独占创建和并发不覆盖已验证。详细边界见 `HAR-07-5c1-completion-receipt-copy.md`。

### HAR-07.5c2 已实现：完成回执直接进入 Harness Detail

- 具备同 run 的类型化 Harness Receipt 时，New UI 与 Textual TUI 完成卡显示
  `Ctrl+O 查看详情` 和精确 `/harness detail <run-id>`；普通回执不显示虚假入口。
- New UI 从时间线反向选择最近一张有效配对卡，复用既有 `openHarnessDetailRoute()` 发出同 run 的
  Explain/Replay 请求；Textual TUI 复用共享 Slash/Service，不复制详情查询逻辑。
- 权限、Interaction、QuickOpen 和 Modal 保持更高输入优先级；无目标、错配或无效版本时不发后端请求。
- Textual 快捷键可通过 `open_latest_harness_detail` 覆盖且卡片显示实际按键；会话替换/清空会移除
  瞬态目标，新运行不会让仍可见的上一张有效卡失效。
- 真实 New UI 进程键盘闭环、Textual app/Engine sink 和 SQLite Store→Bridge→Node action 已验证。
  详细边界见 `HAR-07-5c2-completion-detail-entry.md`。

### HAR-07.4b2 已实现：安全回执 ACK 与 Cursor Recovery

- ARC-02.5b 已让 New UI 按 session 持久保存稳定 client/stream/cursor，并由 Bridge SQLite
  权威持久接受单调 ACK；
- 空闲 Bridge 重连时，现有精确 session resume 会带 `resume_after_cursor`，窗口内只补发缺失的
  completion/harness receipt，同 cursor 不重复渲染；
- ACK 缺失/不一致、窗口外、错误 stream 和超前 cursor 明确进入无 cursor 的业务 Store 快照；
  只有 Harness/ChatRun 权威均成功读取后才建立并 ACK 新基线，不把缺口伪装成“没有更新”；
- 真实双进程链路验证 cursor 1 ACK 后断线，第二进程只补 cursor 2，并将最终 cursor 2 原子保存；
- 详细合同与边界见
  [ARC-02.5b](../architecture/ARC-02-5b-terminal-event-cursor-recovery.md)。

### 尚未完成

- HAR-07.4b：空闲状态下两类安全回执的 ACK、cursor resend 和窗口外 snapshot 已完成，但活动模型流、
  工具副作用、permission/interaction 与运行中 progress 的恢复仍未建立安全事件合同；活动运行断线继续
  fail closed 到 TUI，不能把 HAR-07.4b 或整个 HAR-07 标记完成。
