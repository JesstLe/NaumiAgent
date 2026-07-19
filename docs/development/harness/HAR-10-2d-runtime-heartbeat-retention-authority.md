# HAR-10.2d Runtime Heartbeat Retention Authority

## 目标

为 HAR-10.2c 每次 Bridge 启动产生的独立 runtime heartbeat subject 提供有界、可审计的清理原语，避免长期
进程 churn 让 `harness_heartbeats` 无限增长。本切片只建立 Store authority，不把它隐式塞进 Session retention，
也不提前实现 Supervisor。

## 删除资格

`HarnessStore.prune_runtime_heartbeats()` 在一个 `BEGIN IMMEDIATE` 事务内完成候选读取、机械健康分类和删除。
记录必须同时满足：

- 当前 workspace 且 `subject_kind=runtime`；
- `observed_at` 严格早于调用方给出的 cutoff；
- 不在受保护 subject ID 集合中；
- `assess_heartbeat(..., assessed_at)` 为 `offline`、`stopped` 或 `failed`；
- 位于本轮 `1..1000` 的有界扫描窗口内。

`healthy`、`starting`、`draining`、`stale`、clock regression 与其他 run kind 全部 fail closed。cutoff 必须早于
assessed time；ID、时间、limit 和最多 500 个保护项都复用现有严格边界，避免触碰不同 SQLite 构建的参数上限。

## 竞争语义

SQLite 写事务让 prune 与 heartbeat pulse 串行化：

- pulse 先提交时，新的 `observed_at` 使记录离开旧 cutoff；
- prune 先删除时，仍活跃 producer 的下一 sequence 会重新插入自己的独立 subject；
- 清理 receipt 只证明本事务删除了哪些旧 snapshot，不声称终止 worker，也不阻止合法 producer 重新出现。

返回的 `RuntimeHeartbeatPruneReceipt` 只含 workspace、cutoff、assessed time、scan limit/count、删除 ID 和保护 ID，
不包含异常正文、进程环境或数据库路径。

## 验收证据

- 同 workspace 的 old offline/stopped runtime 被删，fresh runtime、受保护 runtime 与 Pursuit heartbeat 保留；
- limit=1 只删除一个最旧候选，后续调用可继续推进；
- stale 但尚未 offline 的 running heartbeat 不删除；
- 两个独立 Store 并发 prune/pulse 后 runtime 最终存在且 sequence/observed_at 为最新值；
- 非法 cutoff、limit、protected ID 数量与时间格式在写事务前拒绝；
- Ruff、compileall、4 项 retention 测试及 12 项既有 heartbeat authority 测试通过，未运行全量测试。

## 当前边界

本切片没有决定默认保留天数、没有 worker list/cursor，也没有接入 Session retention periodic service。下一切片
应先实现有界 runtime worker catalog，让周期清理能够获取“当前实例保护集合”和可观察 receipt，再决定配置项与
调度；不能让 UI 直接执行无保护的 DELETE。
