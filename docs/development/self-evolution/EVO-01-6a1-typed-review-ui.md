# EVO-01.6a1 Typed Candidate Review UI

## 目标

把 EVO-01.6a 的共享只读 Review Service 通过受 ARC-03 治理的 typed JSONL 协议送入默认新 UI，
提供真正的全屏 Candidate 列表与详情，而不是让前端解析 ANSI/Markdown。TUI fallback 继续调用同一
Service 的共享 renderer，不创建第二套 Store 查询。

## 协议

- Client：`evolution/review/request`
- Server：`evolution/review`
- schema version：1
- owner：`evolution`
- persistence：request `never`，response `snapshot`
- `payload.items` 与 `payload.selected` 继续声明 required redaction

请求支持 list/detail、query、risk、source_kind 和 1..100 limit；Candidate ID、枚举、长度和控制字符
在 Python protocol normalization 层校验。响应最多包含 100 个列表项、100 个审计事件、200 个
Evidence 引用以及每个维度 50 个唯一值。

## 新 UI 行为

- `/evolution` 或 `/evolution list ...` 打开瞬态全屏列表，不向模型提交聊天消息。
- 上下键/Home/End 选择，Enter 打开详情，`b` 返回列表，`r` 只读刷新，Esc 恢复原对话滚动锚点。
- 列表按 risk 与 decision 使用语义色，同时保留完整文字标签。
- 详情显示 Eligibility Gate、硬阻断、机械指标和审计链，并始终显示“实验资格 否”。
- 详情消费 HAR-09.2a 同一聚合对象，显示稳定时间窗、趋势和 Provider/Model/Platform/source 分布。
- 页面状态不持久化；显式 resume 会清除旧 Candidate snapshot 并返回 conversation。
- Bridge/Store 失败返回固定 `evolution_review_failed`，不泄露数据库路径或异常正文。

## TUI parity

Textual TUI 和保留的 legacy CLI 继续通过 `/evolution` 调用 `EvolutionReviewService` 与共享 Markdown
renderer。它们采用线性降级而非复制 Node 全屏视觉，但 list/detail、过滤、Eligibility 和只读边界一致。

## 验收

- 真实 `AgentEngine + EvolutionCandidateStore + JsonlEngineBridge` 产生 typed detail，读取前后 audit
  events 不变。
- Python payload、client request、event registry exact coverage 和固定错误路径有 focused tests。
- Node normalizer 丢弃未知/private 字段并拒绝非法 schema、mode、ID、枚举和类型。
- Node list/detail 在 80/120/200 列不溢出；loading/empty/missing/detail 均有中文状态。
- 命令打开页面、选择、详情、刷新、返回和 resume 清理均为本地状态，不产生 submit 事件。

## 非目标

本切片不实现 approve/reject/defer，不写 Candidate，不提供排序权重，也不把 Candidate 混入 Workbench
approval。动作仍等待 EVO-01.6b、HAR-09.5 与 EVO-02 experiment contract。
