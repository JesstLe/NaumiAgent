# ARC-03 协议版本与兼容治理

## 目标

把当前 JSONL 事件清单升级为版本化 schema registry，覆盖 Runtime control、UI event、Tool、
Artifact、Harness 和 interaction，防止 Python/Node/TUI 各自漂移。

## 子模块

- ARC-03.1 Envelope：protocol/version/type/id/timestamp/run/session/payload。
- ARC-03.2 Schema registry：每种事件 JSON Schema、owner、stability、敏感字段。
- ARC-03.3 Compatibility：major/minor/patch、required/optional field、unknown event 行为。
- ARC-03.4 Negotiation：hello capabilities、最低/最高版本、feature flags。
- ARC-03.5 Ordering：per-run sequence、global cursor、dedup id、gap recovery。
- ARC-03.6 Code generation：Python/TypeScript enum/type 从 schema 生成或验证。
- ARC-03.7 Conformance suite：golden valid/invalid/old/new fixtures。

## 验收标准

- Python 和 Node 对所有 schema fixture 结论一致。
- minor 新增 optional 字段旧客户端继续工作；缺 required 字段明确拒绝。
- 未知非关键事件可忽略并审计；未知关键事件中止相关操作而非整个 Runtime。
- sequence gap 触发 snapshot，不静默遗漏权限/终态。
- schema 中标记 secret/raw/reasoning 的字段禁止进入持久 UI/Harness channel。
- protocol contract 随打包产物发布并可查询版本。

## 分阶段实现

- ARC-03.2a 全事件治理注册表：已实现。发布 contract 精确覆盖全部 client/server 事件，
  为每种事件声明 owner、stability、criticality、persistence、sensitive fields 与 redaction
  义务；Python/Node 在加载时做同语义校验，Bridge 运行状态公开确定性摘要与事件数量，
  新 UI `/debug` 可查询摘要。设计、边界与证据见
  `ARC-03-2a-event-governance-registry.md`。
- ARC-03.2b1 Interaction payload boundary：已实现。interaction request/response/resolved 在 Node 发送端、
  Python 入站端和 Node 接收端执行有界字段、字段组合、稳定 ID 与私有字段投影校验；见
  `ARC-03-2b1-interaction-payload-boundary.md`。
- ARC-03.4a 启动协议协商：已实现。Python/Node 使用同一发布 contract 校验版本区间与能力，
  hello ACK 返回确定性协商结果，失败使用 typed error；新 UI 在成功 ACK 前排队输入且不启动心跳。
  设计与证据见
  `ARC-03-4a-hello-negotiation-design.md` 与
  `ARC-03-4a-hello-negotiation-implementation-plan.md`。
- 其余 Envelope、permission/receipt payload schema、完整机器可读 Schema registry、Compatibility、
  Ordering、Code generation 与 Conformance suite 仍保持 planned；不得因 3.2a/3.2b1/4a 完成而把
  ARC-03 整体标记为完成。
