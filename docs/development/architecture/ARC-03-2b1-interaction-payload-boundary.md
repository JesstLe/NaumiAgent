# ARC-03.2b1 Interaction Payload Boundary

## 目标

在 HAR-10.6 写入持久 interaction authority 之前，先把 `interaction/request`、`interaction_response` 和
`interaction/resolved` 的实时协议边界收紧。未经校验的用户正文、歧义答案或私有字段不得进入 Bridge
pending state、Node UI state，后续也不得直接进入审计或恢复存储。

本切片保持 protocol version 1 和现有交互体验，不引入第二套问题模型。

## 边界规则

- request ID 必须是 `ask-` 前缀的稳定有界 ID；
- request 必须包含 2..3 个唯一选项，标题、问题、label、value、description 和自定义标签均有独立上限；
- `allow_custom` 必须是真实布尔值，request 状态只能是 `needs_input`；
- option response 必须且只能携带 `value`；custom response 必须且只能携带非空 `custom_text`；
- resolved 状态只能是 `answered`，并必须有最终 label；
- 控制字符被拒绝或清理，未知/private 字段不会进入规范化 payload；
- Python 仍会把 option value 与原始 pending request 再次匹配，协议校验不能替代业务关联校验。

## 双端执行

- Node `createEventSender` 在写 JSONL 之前验证并投影 `interaction_response`，非法答案不会写入 Bridge 管道；
- Python `normalize_client_record` 在事件分派前执行相同的 kind/字段组合、长度和 ID 检查；
- Bridge 使用现有 `normalize_interaction_request/response` 进行第二层领域校验；
- Node 收到 request/resolved 时严格校验并只保留声明字段，不能依赖宽松字符串强转修复坏事件；
- TUI 继续复用 Python 领域模型，不需要独立复制一套 schema。

## 验收标准

- option/custom 两种合法答案可完整往返；
- 未知 kind、空 answer、option+custom 歧义组合、非法 request ID 在发送端或 Bridge 边界被拒绝；
- request 少于 2 个选项、重复 value、非布尔 allow_custom 和非法状态被 Node 拒绝；
- private 字段不会进入规范化 request/response；
- 并行问题仍按 request ID 隔离，排队、回答与取消行为不回归；
- 仅运行 interaction Python 子集、Node protocol 单模块、Node interaction state 子集与 JS 语法检查。

## 明确未完成

- 本切片不是 JSON Schema 注册表或自动类型生成器；
- request/answer 尚未写入 durable append-only authority，进程关闭仍会释放当前 Future；
- 没有 timeout、takeover、answer fencing、重开重放或审计 redaction executor；
- permission 与 Harness receipt 的 ARC-03.2b 高风险 payload schema 仍未实现。

下一切片可安全进入 HAR-10.6a：建立单一 durable interaction authority，并让 Pursuit checkpoint 只引用稳定
interaction ID；UI-18.4 再消费该 authority 的只读 pending snapshot。
