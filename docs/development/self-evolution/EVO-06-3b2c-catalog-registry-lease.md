# EVO-06.3b2c Catalog-only Registry Lease

## 目标

在 ARC-01.3f 可撤销注册表原语和 EVO-06.3b2b 真实 ARC-04 Execution Receipt 之间增加一层短期、
可撤销、可审计的目录租约。该租约只声明“临时 namespace 已被当前 Runtime 保留”，不会 import 候选
Artifact，不会加入模型工具 schema，也不授予 Shadow、执行或推广 authority。

这是 EVO-06.4 Shadow evaluation 的最小前置，不是 Limited Activation。

## Authority 输入

获取租约时必须重新验证同一 Candidate 的全部 current facts：

1. Sandbox Execution Request 仍指向当前 Git revision、tree、Artifact 与 Binding；
2. Implementation Artifact 仍是 `preview_ready`，ID 与摘要和 Request 完全一致；
3. Execution Receipt 为 `passed`，request ID/摘要完全一致；
4. 所有场景机械通过，permission observation 完整；
5. 当前调用持有与 `action/candidate_id/duration_seconds/run_id` 精确匹配的持久权限回执。

任一输入缺失、漂移或篡改均 fail closed，不能用旧 passed 文案代替 current authority。

## Lease 与状态回执

`EvolutionCapabilityRegistryLease` 是 content-addressed 不可变对象，封存 Request、Receipt、Artifact、
临时工具名、Runtime identity、父权限回执、签发/到期时间与 30..900 秒租期。固定安全位为：

- `catalog_only=true`；
- `registry_reserved=true`；
- `model_visible=false`；
- `shadow_authorized=false`；
- `executable=false`。

状态使用 append-only、前序摘要串联的 `EvolutionCapabilityRegistryLeaseStateReceipt`：

- `active/acquired`：来源 current、Runtime ownership 和本地 reservation 都已证明；
- `released/user_released`：只允许 owner Runtime 显式释放，且必须证明 compare-and-release 成功；
- `expired/lease_expired`：到期检查重新记录三个来源是否仍 current；
- `revoked/*_revoked`：Request、Artifact 或 Execution Receipt 漂移；
- `revoked/registry_reservation_lost`：本 Runtime 的 reservation 被异常移除。

SQLite 的 `current_state` 并非可信缓存：恢复时必须与签名式 state receipt 一致；lease JSON、state JSON 和
各自持久摘要任一不一致都拒绝读取。active Candidate 与 active 临时工具名均有数据库唯一索引；写入采用
`BEGIN IMMEDIATE`，跨 Store 竞争只有一个胜者。

## Registry 隔离

`ToolRegistry.reserve_unique()` 在同一 `RLock` 临界区检查 exact name 和 `default.foo`、
`default__foo` legacy alias。reservation：

- 不可被 `get()`、`get_exact()`、`names`、`all()` 或 `get_openai_tools()` 解析；
- 会阻止可信 replace 注册与动态 unique 注册覆盖名称；
- 只能由相同 owner ID compare-and-release，防止旧 lease 删除新 owner；
- 本切片绝不 materialize 或 import Candidate source。

另一个进程看到未到期 lease 时显示 `detached`：它既不恢复本地 reservation，也不获得任何执行权。这样
不会误把并发存活 Runtime 当作崩溃进程。原进程退出后其内存 Registry 自然消失；持久 lease 到期后由
任一检查者机械收口。跨进程存活判定和主动接管必须由后续 heartbeat/fencing 切片提供，不能在这里猜测。

## 双通道与界面

- Agent Tool：`evolution_capability_registry_lease`，支持 `inspect/acquire/release`；变更操作要求持久权限回执；
- CLI/TUI fallback：`/evolution capability-lease`、`capability-register [30..900]`、
  `capability-unregister`；
- New UI：同名 typed actions，经严格协议归一化后调用同一 Tool；
- Bypass：不二次确认，但仍形成可验证的 BYPASS permission receipt，不绕过 lease authority；
- 所有界面都明确渲染 Catalog、模型可见、Shadow 和可执行四项状态。

## 验收证据

- [x] exact/legacy alias 冲突、八线程 reservation 竞争、compare-and-release 小模块测试；
- [x] 真实 ARC-04 Worker 形成 passed Receipt 后才可获取 lease；
- [x] 显式释放、重新获取、源码漂移动态撤权和 30 秒到期均形成串联回执；
- [x] 候选临时名称不进入 Tool lookup、模型 schema 或公开 names；
- [x] 其他 Runtime 只能看到 detached，不能获取同一 active lease；
- [x] `current_state`、lease/state JSON 摘要篡改均 fail closed；
- [x] Agent Tool、CLI/TUI fallback 与 New UI typed action 使用同一底层服务；
- [x] 仅运行相关 Ruff、py_compile、Python/Node 小模块测试和真实 E2E，不运行全量测试。

## 非目标与下一步

本切片没有加载候选类、构造 Tool 实例、发送模型 schema、记录 shadow recommendation 或产生副作用。
[EVO-06.4a](EVO-06-4a-sealed-shadow-descriptor.md) 已消费 active catalog lease 构造不可执行、永久未授权的
Shadow descriptor。下一步是 4b isolated observation contract；不得直接进入 Limited Activation。
