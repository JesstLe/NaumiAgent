# ARC-04.1a Worker 能力、健康与准入合同

## 交付目标

在 Runtime 把 Tool、Browser 或 Agent job 交给隔离 daemon 之前，先建立一个可机械验证、默认拒绝的
worker admission 边界。该边界必须回答五个问题：worker 是谁、运行在哪个平台、声明了哪些能力、可承载
多少资源、当前是否健康且仍有容量。

本切片只完成合同和判定器，不启动 daemon、不执行用户命令，也不授予 permission。这样可以先冻结
ARC-04.2/04.3 和 HAR-08.4 必须消费的输入形状，同时避免把本机 `subprocess` 或临时目录误称为沙箱。

## 代码所有权

- `src/naumi_agent/daemons/worker_contract.py`：合同、摘要、平台探测、健康绑定和准入判定。
- `src/naumi_agent/daemons/__init__.py`：稳定导出面。
- `src/naumi_agent/harness/heartbeat.py`：继续作为唯一 liveness 分类来源；本模块不复制心跳状态机。
- `tests/unit/test_worker_contract.py`：合同边界与拒绝路径。

## 小模块

### ARC-04.1a.1 Worker 身份与版本

`WorkerContract` 固定 `worker_id`、进程 incarnation `instance_id`、fencing `epoch`、worker kind、协议区间、
软件版本和签发时间。合同是 frozen dataclass；能力集合会排序，输入包含重复项或无效标识符时立即拒绝。

验收：

- Tool、Browser、Agent kind 使用明确枚举，不能以任意字符串穿透。
- `protocol_min <= protocol_max`，协议范围和 epoch 均有上界。
- 修改任一已签发字段后 `verify_worker_contract()` 返回 false。

### ARC-04.1a.2 跨平台事实

`detect_worker_platform()` 将 macOS、Linux 和 Windows 归一为 `darwin`、`linux`、`windows`，同时记录
machine、Python implementation 和 Python version。探测不依赖 cwd，也不保存用户目录。

验收：

- Darwin/Linux/Win32 可形成稳定合同；未知系统拒绝注册。
- 平台 allowlist 不排序、重复或包含未知值时拒绝创建 admission request。

### ARC-04.1a.3 能力与隔离声明

能力不是自由文本，而是 `WorkerCapability` 枚举。隔离声明显式覆盖：ephemeral workspace、默认断网、
环境变量 allowlist、资源限制、进程树取消和 artifact digest。每个为 true 的隔离声明必须存在对应能力；
Shell、Browser、Agent 专属能力也必须与 worker kind 一致。

验收：

- 隔离布尔值与能力不一致时，合同无法签发。
- admission requirement 中任一所需能力或隔离保证缺失时 fail closed。
- 未声明的保证不能由路径名、平台或 worker kind 推断出来。

### ARC-04.1a.4 资源与容量

`WorkerResourceEnvelope` 声明最大并发、内存、CPU 时间、墙钟时间和输出字节数，并对每项设置现实边界。
`WorkerHealthReport` 把 active job 数、是否接收新 job、原生 Harness heartbeat 和精确合同摘要绑定在一起。

验收：

- active jobs 达到 `max_concurrent_jobs` 时拒绝新 job。
- draining、stale、offline、failed、starting 或显式不接单时均拒绝。
- 资源下限不满足时返回 `resource_insufficient`，不静默缩小 job 要求。

### ARC-04.1a.5 可解释准入

`assess_worker_admission()` 一次返回全部机械阻断原因，包括合同/健康摘要错误、身份 generation 不一致、
kind、协议、平台、能力、资源、隔离、健康和容量。只有原因集合为空时返回 `admitted`。

验收：

- 健康报告必须同时匹配合同摘要、worker id、instance id、epoch 和 Harness run kind。
- 心跳年龄只调用 `assess_heartbeat()` 计算，阈值规则保持单一来源。
- 结果包含规范化 `checked_at` 和心跳健康分类，供后续 Runtime/Harness 审计使用。

## 安全边界

- SHA-256 摘要提供稳定内容身份和传输/持久化后的篡改检测，但不是签名，不能证明远端 worker 身份。
- `admitted` 只表示 worker 满足能力与健康前置，不代表 permission grant、workspace lease 或 job 已获授权。
- 当前没有 daemon producer、受信注册表、mTLS/本机凭据、进程隔离或资源控制器；Runtime 不得据此执行 job。
- `bypass` 将来仍必须形成带 run/scope/expiry 的显式 grant，不能绕过 worker contract 或 job fencing。

## 验证记录

- Ruff：`ruff check src/naumi_agent/daemons tests/unit/test_worker_contract.py tests/unit/test_harness_heartbeat.py`
- Unit：`pytest -q tests/unit/test_worker_contract.py tests/unit/test_harness_heartbeat.py`
- Real smoke：使用当前宿主机 `platform` 事实签发 Tool worker 合同，生成健康报告并完成一次纯判定 admission；
  不创建子进程、不访问网络、不修改工作区。

## 后续依赖

ARC-04.1b 已实现 Runtime-owned worker registration store 与 incarnation/fencing authority，使 admission 不再
选择调用方传入的合同。下一步 ARC-04.2 应把 immutable job、permission grant、workspace lease 和
idempotency key 绑定到已注册 worker。ARC-04.3 的真实 shell daemon 完成并证明进程树、网络、环境和资源
隔离之前，HAR-08.4 继续保持 planned，EVO-03.6 也不得运行项目命令。
