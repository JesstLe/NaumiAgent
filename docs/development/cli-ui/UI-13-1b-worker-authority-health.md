# UI-13.1b Worker Authority Health

## 1. 目标

把 ARC-04.1b 的 durable Worker registration authority 与 HAR-10.2a 的 typed heartbeat 投影到现有
Doctor Health 模型，让新 UI 与 TUI 都能区分“尚未启动 Worker”“注册正常”“心跳不可信”和“注册库损坏”。
本切片只增加只读诊断，不启动 Worker、不派发任务、不迁移数据库，也不把 heartbeat 当成执行授权。

## 2. 权威输入与边界

- Worker Registry v1 提供 active incarnation 的合同、epoch、kind、平台和最大并发容量；
- Harness Store v16 提供相同 workspace、worker id 与 kind 的最新 heartbeat；
- 合同摘要和 Registry 索引列必须重新校验，不能只信查询列；
- heartbeat 必须匹配合同的 `worker_id + kind + instance_id + epoch`，再由 `assess_heartbeat()` 判定
  healthy/stale/offline/starting/draining/stopped/failed/clock_regression；
- heartbeat 只证明活性，不包含 signed capacity report，界面只显示合同最大容量，不伪造 active jobs 或
  accepting jobs；
- Worker Registry 缺失表示尚未按需启动，属于正常状态；active registration 缺 heartbeat 则 fail closed。

## 3. 严格只读探针

`inspect_worker_authority_health()` 使用 SQLite `mode=ro + query_only`，不会调用 Store 的 schema 初始化或迁移
路径。Registry/Harness 文件缺失时不创建父目录、数据库或默认行；未来 schema、损坏数据库、错误文件
类型和锁超时都返回有界机械状态，不覆盖、不降级迁移、不输出底层异常正文。

探针最多展开 5 个 active Worker，并只把前三个摘要送入 Doctor 文本；超出上限明确标记截断，避免损坏或恶意
Store 制造无界内存和终端输出。公开摘要不包含 contract JSON、heartbeat detail code、环境、secret、用户正文或
绝对 Store 路径。

## 4. UI/TUI 行为

`run_doctor()` 增加 `Worker authority` 检查，因此：

- 新 UI 从既有 `doctor/health` typed snapshot 得到 runtime domain、severity、归因、详情和下一步；
- TUI 从同一 `DoctorReport` 的 Markdown fallback 得到相同事实；
- 无 Registry：正常，提示首次真实注册时按需创建；
- Registry 正常且没有 active Worker：正常；
- 全部展开 Worker heartbeat healthy：正常；
- starting/draining 或列表截断：受限；
- stale/offline/stopped/failed/clock regression/missing/identity mismatch/invalid/unavailable：错误，并提示暂停派发；
- Registry 损坏、未来版本或错误类型：产品运行时错误，禁止自动修复。

颜色只是辅助；状态、平台、epoch、容量和心跳结论都以文字表达。Bridge heartbeat 仍表示 UI 控制面连接，
Worker heartbeat 表示执行实例活性，两者不得合并成一个“在线”灯。

## 5. 验收证据

- absent Registry/Harness 不产生文件或目录；
- 真实 WorkerRegistryStore + HarnessStore SQLite 可跨关闭后被只读组合；
- healthy、stale、错误 instance、缺 heartbeat、未来 Harness schema 有确定性状态；
- corrupt/future/wrong-type Registry fail closed，且探针前后文件字节不变；
- 合同的 epoch、platform、machine、最大并发和 heartbeat age 正确进入用户文案；
- heartbeat `detail_code` 不进入公开 Doctor 结果；
- typed Health 将 Worker authority 映射到 runtime domain，错误归因 product runtime；
- 只运行 Worker authority、Registry、Doctor 与 typed Health 小模块测试，不运行全量测试。

## 6. 当前不足与后续

- ARC-04.2a 已持久化 execution-scoped grant 并绑定 Harness Tool lease；immutable ToolJob 与跨进程
  completion receipt 仍未完成，因此本页不能声称 Worker 已可安全执行任务；
- durable `WorkerHealthReport` 尚不存在，当前无法可信显示 active jobs、accepting jobs、队列深度或资源实耗；
- 只有 latest heartbeat，没有 jitter、丢包率、crash-loop 历史和 SLO；这些属于 ARC-08；
- 当前真实 producer 仍以 Pursuit 为主，Tool/Browser/Agent daemon heartbeat producer 要随各 daemon 垂直切片接入；
- 下一步应在跨文档依赖中选择最小用户交付：若 ARC-04.2 的 grant/lease authority 已就绪，再做 ToolJob；否则
  优先推进 UI-13 稳定 provider 错误码或 HAR-10 的下一项真实 producer，不能伪造授权闭环。
