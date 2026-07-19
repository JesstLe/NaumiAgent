# ARC-04.2a Scoped Execution Grant Authority

## 1. 交付目标

为未来 ToolJob 建立可跨进程验证、短期有效、不可扩大范围的持久执行授权。它不是现有
`PermissionGrantStore` 的持久化版本：session grant 只按 session/tool family 放行交互确认，而 execution grant
必须绑定一次具体调用、参数摘要、Tool run lease 和 active Worker incarnation。

本切片不启动 daemon、不执行命令，也不把 ARC-04.2 标记为完成；immutable ToolJob、job queue、执行回执和
daemon transport 仍需后续切片。

## 2. ExecutionGrantContract

合同 schema v1 绑定以下不可变事实：

- `session_id + run_id + call_id + tool_name + tool_family`；
- canonical JSON `arguments_sha256`，不持久化原始参数或 secret；
- caller `idempotency_key`；同 key 不得换参数、call、Worker 或授权来源；
- workspace 规范路径的 SHA-256，不向 worker authority Store 写入明文路径；
- Harness `tool` run lease 的 owner、epoch；
- Worker Registry active contract 的 worker/instance/epoch/contract digest；
- Permission mode、来源（policy/user confirmation/session grant/bypass）和有界 reference；
- `issued_at/expires_at`、request digest 和完整 grant digest。

grant 最长 300 秒，且 expiry 不得超过当前 Tool lease。`bypass` 表示 Runtime 不再询问用户，但仍必须生成上述
具体 scope、lease、Worker epoch 和 expiry；它不是无限期 bearer token。

## 3. Authority 签发

`ExecutionGrantAuthority.issue()` 依次验证：

1. PermissionDecision 已允许，source 与 mode/outcome 一致；同时读取 UI-12.3a durable decision receipt，核对
   session/run/call/tool/参数摘要/mode/source/outcome；缺失、拒绝或任意字符串 reference 均拒绝；
2. Worker Registry 存在 active `Tool` Worker；Browser/Agent contract 不可用于 Tool grant；
3. Harness 存在同 workspace/run 的 active `tool` lease；lease owner 必须等于 active worker instance；
4. lease 尚未过期，grant expiry 取 TTL 与 lease expiry 的较早者；
5. 参数可做 bounded canonical JSON；嵌套非字符串 key、NaN/Infinity、超过 256 KiB 均拒绝；
6. SQLite 用 idempotency key 串行签发：同一 request 返回首次 grant，不同 request 冲突且不覆盖。

Runtime Composition Root 惰性装配 `ExecutionGrantStore` 与 `ExecutionGrantAuthority`；构造 Engine 不创建目录或
数据库。Store path 固定为 `runtime_data_dir/execution-grants.db`，并登记到 Store Catalog。

## 4. 再验证与撤销

daemon 接收 ToolJob 前必须用 grant id 与实际 request 再验证：

- grant 不存在、已撤销或过期；
- session/run/call/tool/arguments/idempotency/worker/workspace 任一变化；
- active Worker contract 已由更高 epoch fencing；
- Tool lease 缺失、released、expired 或 owner/epoch 变化；

任一条件返回机械 reason 并 fail closed。SHA-256 是内容完整性，不是跨主机签名；未来 Runtime Service transport
仍必须认证本机调用者。撤销保留 immutable contract 和撤销原因，不删除审计行。

## 5. 持久化与隐私

- 独立 ExecutionGrant SQLite schema v1；其依赖的 Permission Decision Store 已升级 schema v3，并保持
  v1/v2 receipt 摘要兼容。父目录/数据库均在首次签发时按需创建；POSIX 分别收紧为 0700/0600；
- 未版本化非空库、未来 schema、损坏合同、索引列不一致和错误路径类型拒绝接管；
- contract JSON 仅含参数 digest，不含 command、环境变量、reason 正文、用户消息或 API key；
- grant id、request digest 与 contract digest 分别校验，篡改不能降级为“缺失”；
- Store Catalog 将其登记为 restricted、audit-long-term 的 `runtime.execution_grants`。

## 6. 验收证据

- 真实 WorkerRegistryStore + HarnessStore + ExecutionGrantStore 完成 register→lease→issue→关闭→reopen→validate；
- 参数顺序变化保持同 digest；参数内容变化复用同 idempotency key 被拒绝；
- 8 个并发相同签发请求只产生一行和一个 grant id；
- 参数变化、过期、撤销、Worker takeover、lease release/missing/wrong owner 均 fail closed；
- 非 Tool Worker、伪造 bypass、伪造 confirmation、NaN、超限和嵌套非字符串 key 被拒绝；
- 原始参数 secret 不出现在数据库 bytes 或 contract JSON；
- tampered contract、future schema、wrong-type path 均拒绝；
- Composition Root、RuntimePaths、Resource override 与 Store Catalog 使用同一个路径事实源；
- 只运行 ExecutionGrant、Worker/Lease、Composition、Catalog、Doctor 和 ownership 小模块测试。

## 7. 当前不足与下一切片

- ARC-04.2b 已让 `ImmutableToolJob` admission 消费本 grant，ARC-04.2c 已补齐 durable lifecycle 与
  idempotent terminal receipt；ARC-04.3a 已提供当前 non-PTY Shell 执行 Worker；
- UI-12.3a 已提供跨重启 confirmation decision receipt；UI-12.3b1/3b2 又提供 direct-allow 父回执、有限
  委托范围与精确短期子回执，因此 policy/delegated execution grant 均可验证签发；
- Worker admission 的能力/隔离/heartbeat/capacity 仍需与 grant validation 同时通过，二者不可互相替代；
- grant、Worker Registry、Harness lease 位于三个 SQLite Store，签发不是跨库原子事务；每次消费重读三个
  authority，以 fencing 抵御签发后的 takeover；未来 ARC-02 可用 Runtime Service 单写者减少竞态窗口；
- ARC-04.3b/HAR-08.4b 已组合 Worker registration、Tool lease、grant、ToolJob admission 并切换生产
  `/harness check`；下一消费者切片应复用该链编排 Sandbox Suite/Batch 或成对 cohort。
