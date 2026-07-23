# ARC-03.3a Additive Informational Event Compatibility

## 1. 目标

为 HAR-07.4b reconnect recovery 建立最小的前向兼容前置：同一 JSONL major version 的未来
Bridge 可以增加不改变控制或终态的 `informational` 事件，而当前 New UI 必须：

1. 只在未来 Bridge 明确证明当前 registry 摘要仍兼容时接受 additive registry；
2. 消费未知 informational 事件的 sequence，避免下一条已知事件被误判为 gap；
3. 丢弃整个未知 payload，只审计有界 envelope 元数据；
4. 对未证明兼容、缺少 criticality 或声明为 control/terminal 的未知事件继续严格拒绝。

该切片只解决“可证明的新增信息事件”，不实现 cursor、ack、Event Store、Bridge 自动重连或未知关键
事件的局部恢复状态机。

## 2. 为什么是 HAR-07.4b 的前置

ARC-03.5a 已能检测 sequence gap，但当前 UI 过去会在 payload normalization 前拒绝任何未知事件。
即使未来事件只是进度提示，它的 sequence 也不会进入 guard，后续已知 Receipt/Snapshot 会被误判为
丢失。与此同时，原有 registry digest 必须完全相同，无法区分“安全增加 informational event”和
“已有控制语义被破坏”。

直接实现 reconnect 会把这种兼容冲突带入重放流：客户端可能因一个可忽略的新事件反复退出，或为了
继续运行而无证明地放宽全部 digest。ARC-03.3a 先建立机械兼容证明，不提前实现恢复传输。

## 3. Compatibility Ledger

发布 contract 新增：

```json
{
  "compatibility": {
    "previous_registry_sha256": [],
    "unknown_informational_events": "ignore_and_audit"
  }
}
```

当前 registry digest 仍只覆盖 event governance policy 与 capability binding，避免 ledger 自引用。
加载时形成：

```text
compatible_registry_sha256 = [current_digest, ...previous_registry_sha256]
```

未来版本只有在以下条件全部成立时，才可把旧摘要加入 `previous_registry_sha256`：

- 旧客户端已知事件的 criticality、敏感字段和 redaction 义务保持兼容；
- 新增 server event 仅为 informational，或旧客户端不会收到新增 control/terminal event；
- 新增 client event 不成为旧客户端的必需启动能力；
- Python/Node conformance 和真实旧 UI 进程验证通过。

Breaking change、已知事件降级 redaction、改变 terminal/control 语义或删除事件时，禁止登记旧摘要。
ledger 最多保留 31 个旧摘要，值必须唯一且不能重复当前摘要。

## 4. Registry 协商

Bridge `ready/runtime/status.protocol_registry` 新增 `compatible_registry_sha256`。New UI：

- digest、事件数量完全相同：`exact`；
- Bridge digest 不同，但兼容列表包含当前 UI digest，且事件数量不减少：`attested_additive`；
- 未包含当前摘要、事件数量减少、版本不同、摘要畸形或列表重复：拒绝状态记录。

兼容是 Bridge 对旧 registry 的显式证明，不由客户端根据“看起来只多了一个事件”猜测。
`/debug` 会显示“精确匹配”或“兼容新增”，便于定位实际运行组合。

## 5. Envelope 与未知事件处理

Python Bridge 从发布 registry 为每条 server envelope 写入 `criticality`。New UI 对已知事件允许旧
Bridge 缺失该字段；一旦出现，就必须和内置 policy 完全一致。

未知事件处理顺序：

1. 严格校验 version、type、payload 对象、sequence 和 `criticality=informational`；
2. normalization 立即把 payload 替换为空对象，并丢弃未知的顶层字段；仅保留有界 type/id/request_id/
   sequence/criticality，不允许任意内容进入 state；
3. sequence guard 正常消费该序号；
4. flush 此前已排队的 Ready，读取已接受的 registry compatibility；
5. 只有 `attested_additive` 才记录 `protocol.unknown.informational` 并静默继续；
6. exact/缺失 attestation 时记录 `protocol.unknown.rejected`，显示中文协议提醒，但仍消费序号以避免
   级联伪 gap；
7. 未知 control/terminal 或缺 criticality 继续进入严格协议错误路径。

raw JSONL frame 和 protocol error 不再写入 debug log；日志只记录 frame bytes、type、error 和脱敏后的
envelope 元数据。未知 payload 即使包含 secret 也不会进入 debug、timeline 或持久 UI snapshot。

## 6. 跨表面边界

- New UI 是 JSONL consumer，执行 additive compatibility。
- Textual TUI 与 Engine 同进程，不接收未知 JSONL server event，因此本切片不伪造 TUI 协商。
- Python Bridge 仍只能发送自己 registry 已登记的事件；`emit()` 对未知事件失败关闭。
- Harness Receipt、permission、interaction 和 run terminal 仍是 terminal/control 事件，不能借
  informational 路径绕过。

## 7. 验收标准

- Python/Node 都拒绝非法、重复和当前摘要自引用的 ledger；
- Bridge status 发布当前摘要组成的 compatibility 列表；
- Bridge 并发 emit 的 sequence 顺序不变，且 envelope criticality 来自 registry；
- Node 接受显式 attested additive registry，拒绝无 attestation 或事件数回退；
- 未知 informational normalization 丢弃整个 payload；
- 真实 Node UI 子进程收到 `ready → unknown informational → runtime/status` 后无 sequence gap，后续状态
  正常显示，debug 中不存在 secret；
- exact registry 冒充新增 informational event 时有可见提醒、无 payload 泄漏、无伪 gap，进程继续；
- 未知 terminal/control 仍被拒绝；
- 仅运行 ARC-03.3a 相关小模块测试，不以该结果代表完整 ARC-03 或 HAR-07.4b。

## 8. 未完成

- ARC-03.3b：未知 control/terminal 事件按 request/run scope 局部隔离与恢复；
- ARC-02.5：持久 Event Store、cursor、ack、bounded replay 和 slow-client snapshot；
- HAR-07.4b：Bridge 重新协商后按 revision/gap 幂等补发 Receipt/Explain/Replay；
- UI-17：断线 uncertain、权限中断恢复和完整发布矩阵。

下一切片应返回 Harness/Runtime reconnect 依赖审计，不继续线性扩张 ARC-03。
