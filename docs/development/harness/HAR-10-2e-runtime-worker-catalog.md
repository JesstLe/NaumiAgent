# HAR-10.2e Runtime Worker Catalog

## 目标

为 runtime heartbeat 提供 workspace 隔离、有界、可跨 Store reopen 的只读目录。该目录是 retention 调度、
Doctor worker 页面和未来 Supervisor 的共同数据入口，但不授予 takeover、kill、retry 或 prune 权限。

## Page 合同

`HarnessStore.list_runtime_heartbeats()` 返回 `RuntimeHeartbeatCatalogPage`：

- `workspace_root` 与调用方规范路径一致；
- `assessed_at` 为本轮所有页固定的 UTC 评估时间；
- `items` 最多 200 个 typed `HarnessHeartbeatSnapshot`；
- `next_cursor` 为空表示结束，`has_more` 只是其布尔投影。

目录只读取 `subject_kind=runtime`，按 `observed_at DESC, subject_id ASC` 排序。相同时间的 worker 仍有稳定顺序，
并新增 `idx_harness_heartbeats_catalog` 覆盖混合方向排序；索引通过既有幂等 schema 初始化补入，不提升数据库版本。

## Cursor 边界

opaque cursor 使用 canonical JSON + SHA-256 envelope，再做 URL-safe Base64 编码，最大 1024 字符。payload 绑定：

- schema version；
- workspace 路径摘要，不暴露本机路径；
- 固定 `assessed_at`；
- 上页最后一条 `observed_at + subject_id`。

解码严格拒绝未知字段、非法 Base64/JSON、摘要变化、版本不兼容、跨 workspace 使用、评估时间变化和非法 ID/时间。
摘要用于完整性检测，不是认证或授权；cursor 只能影响只读翻页位置。

## 一致性语义

静态数据集上 cursor 无重复、无遗漏，并可由新 `HarnessStore` 实例继续。跨请求不持有 SQLite 长事务，因此并发 pulse
采用 read-committed：已经翻过的 worker 更新到更晚时间不会在旧 cursor 后重复，新 worker 或未读 worker 的位置变化
可能要求用户刷新首屏。目录不得声称是跨请求原子快照；固定 assessment time 只保证健康分类阈值一致。

## 验收证据

- 五个 runtime（含相同 observed time）以 limit=2 跨三个 Store 页面得到确定顺序，无重复/遗漏；
- latest worker 为 healthy、old worker 按固定 assessment time 为 offline；
- workspace 之间、runtime 与 Pursuit 之间严格隔离；
- cursor 篡改、跨 workspace、assessment time 漂移、非法 cursor 和 limit>200 被拒绝；
- cursor 产生后新增的更晚 worker 不会让 continuation 回卷，刷新首屏后可见新 worker；
- 空数据库返回空 page，不为查询创建虚假 worker；
- catalog 混合排序索引在真实 SQLite 中存在；
- Ruff、compileall、5 项 catalog 与 16 项 heartbeat/retention 定向测试通过，未运行全量测试。

## 后续衔接

HAR-10.2f1 已在独立 periodic service 中组合 catalog 与 HAR-10.2d prune，并声明独立 runtime RunLease、活跃保护、
删除前续租和稳定回执。该核心仍未接入 Bridge，因此默认不会执行后台删除。HAR-10.2f2 再补配置、Bridge 生命周期与
用户可见状态；Doctor/New UI 展示也应保持只读投影，避免在 catalog 内混入协议代码。
