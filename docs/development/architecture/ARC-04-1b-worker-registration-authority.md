# ARC-04.1b Worker 注册 Authority 与 Incarnation Fencing

## 交付目标

ARC-04.1a 冻结了 Worker 能力、资源、隔离与健康合同，但纯判定函数仍可接收调用方提供的合同。
本切片增加 Runtime 所有的持久 registration authority：调度前必须按 `worker_id` 读取 authority 当前
active incarnation，不能相信健康报告携带的合同选择。

## 状态所有权

- `RuntimePaths.worker_registry_db_path` 固定为 `runtime_data_dir/worker-registry.db`；daemon 不解析 cwd、
  环境变量或用户目录。
- Composition Root 惰性构造唯一 `WorkerRegistryStore`，并通过 `RuntimeResources` 注入对象图。
- Store Catalog 以 `runtime.worker_registry` 登记 schema、owner、敏感级别和长期审计保留策略。
- SQLite schema v1 保存所有 incarnation 历史；不存在时惰性创建，未知未版本化库与未来版本均拒绝接管。

## 注册状态机

每个 `worker_id` 最多一条 `active` 记录；历史记录为 `superseded` 或 `revoked`。

1. 首次有效合同成为 active。
2. 相同 digest 重放返回原记录，不增加行、不重写时间。
3. 只有更高 epoch 且不同 instance id 的合同可以接管。
4. 接管在 `BEGIN IMMEDIATE` 事务中先终结旧记录，再插入新记录；partial unique index 阻止双 active。
5. 低/相同 epoch、instance 复用、issued/registered 时间回退全部拒绝。
6. revoke 必须精确匹配 active worker/instance/epoch；旧 owner 不能撤销新 incarnation。
7. active 被撤销后仍保留历史最高 epoch 水位；终结合同不能复活，未使用过但更低的 epoch 也不能注册。

## Authority-only Admission

`WorkerRegistryStore.assess_admission()` 只接受 `worker_id`、健康报告和 job requirements。它先从 SQLite
选择 active contract，再调用 ARC-04.1a 的纯判定器：

- 未注册或已撤销返回 `registration_missing`；
- 旧健康报告与当前 contract digest/instance/epoch 不一致时返回 `identity_mismatch`；
- 数据库合同 JSON、索引列或 digest 被修改时 authority 读取失败并停止准入，不尝试修复或回退；
- active contract 匹配后仍必须通过协议、平台、能力、资源、隔离、心跳和容量检查。

## 安全与耐久边界

- SQLite authority 解决的是本机持久 current-pointer、并发串行化和重启 fencing；它没有认证真实 OS 进程。
- SHA-256 仍是内容身份，不是签名。未来 Runtime Service 必须用本机 socket 凭据/nonce 或等价机制认证
  register/revoke 调用者。
- 注册不是 permission grant；`bypass` 也不能伪造 active worker、跳过 job scope 或延长 lease。
- 本切片不创建 daemon producer、不运行 subprocess、不承诺 workspace/network/resource 隔离已经生效。
- schema v1 只允许空库初始化，不迁移未知表；未来升级必须接入 ARC-05 Migration Runner 与备份门。

## 验收证据

- 惰性路径：Composition Root 构造不创建目录或 DB，首次 register 才创建 0700 父目录和 POSIX 0600 DB。
- 耐久性：注册后重开 Store 可恢复同一合同、状态与时间。
- 并发：两个独立 Store 同时注册 epoch 2/3，最终 active 必为最高 epoch，最多一个 active。
- Fencing：旧 epoch 接管、同 instance 新 epoch、旧 generation revoke 均拒绝。
- 完整性：合同摘要、JSON、索引列、未来 schema、未知未版本化库均 fail closed。
- Admission：未注册、旧报告、撤销、健康/资源不满足和成功路径均由 authority 入口验证。
- 仅运行 Worker Registry、Worker Contract、Runtime Composition、Store Catalog 小模块，不运行全量测试。

## 后续

ARC-04.2a 已在此 authority 上增加 execution-scoped grant，并绑定 Harness Tool lease、参数 digest、
idempotency key 与 active Worker epoch。下一步 ARC-04.2b 仍需 immutable ToolJob 与 durable completion
receipt。ARC-04.3 真实 Shell daemon 与 ARC-04.6 Supervisor 尚未完成，HAR-08.4 和 EVO-03.6 仍不能执行项目命令。
