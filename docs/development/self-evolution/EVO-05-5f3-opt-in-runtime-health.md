# EVO-05.5f3 Opt-in Runtime Launch and Health Receipt

## 目标

消费 current [EVO-05.5f2](EVO-05-5f2-opt-in-activation-reconciliation.md) Deployment Receipt，使用
ARC-07 stable launcher 的同一解析/持久化权威启动 exact active candidate 的隐藏 `--runtime-health-check`，并把真实
进程终态冻结为可恢复、可 fencing 的 Opt-in Runtime Health Receipt。

本切片只证明“当前本机 opt-in candidate 的一次受控 health probe 已真实运行并返回 exact healthy report”。它不启动
用户 session，不积累 observation window，不完成 opt-in stage，更不授予 percentage、stable 或 promotion authority。

## 权威链

执行前必须同时满足：

1. 5f2 Deployment Receipt 可从 durable source 重读，且动态 `active_deployment_authority=true`；
2. ARC-07 active pointer、candidate immutable slot、Boot Receipt、trusted builder、HAR opt-in enrollment 和 rollout
   control 仍与 Deployment Receipt 完全一致；
3. `resolve_and_record_launch()` 以 `process_start_requested=true`、`argument_count=1` 形成并持久化 exact Launch
   Resolution；它必须绑定同一 pointer/slot/boot/binary，不能在 pointer race 后启动 stale candidate；
4. runtime 进程内部再次重放 active chain，验证自身 binary path/digest 与 launcher 注入的 slot/generation/install-root。

Launch Resolution 是“获准启动哪个 binary”的事实；Runtime Health Report 是“该 binary 在进程内部验证了什么”的事实；
5f3 Receipt 只在两者 exact 一致时授予 runtime-health authority。

## 并发、崩溃与恢复

Runtime Health Store 使用 SQLite `BEGIN IMMEDIATE` 的 deployment-keyed claim：

- claim 只持久化随机 owner token 的 SHA-256，不落盘 bearer secret；
- lease 未过期时并发调用等待同一 terminal receipt，不重复启动 probe；
- lease 过期后新执行者递增 epoch 并接管；旧执行者即使稍后返回，也无法提交终态；
- process launch 前解析失败会 fenced 删除本 owner 的空 claim，允许立即安全重试；
- process 已启动后的 task crash 保留 claim，lease 到期后 `reconcile()` 可重新运行只读 health probe；
- healthy/unhealthy 都是 immutable terminal receipt，后续调用幂等返回，不隐藏失败也不无限重试。

该恢复模型只适用于无用户副作用的 read-only health machine interface，不能直接复制到普通用户 session 或迁移命令。

## 进程与环境隔离

5f3 通过 `ValidationExecutor` 使用 argv-only 子进程、独立进程组、1 MiB 以下的固定输出上限、timeout/cancel 后完整回收。
环境不继承任意父进程秘密，只从跨平台运行所需的 `PATH`、Windows system/temp、locale/temp 白名单中选择，并注入：

- `NAUMI_ACTIVE_SLOT_ID`
- `NAUMI_ACTIVE_POINTER_GENERATION`
- `NAUMI_INSTALL_ROOT`

API key、模型凭据、SSH/云凭据和任意 `NAUMI_*` 用户变量不会传入 candidate probe，也不会进入 Receipt。Receipt 只保存
captured output text digest、总字节数、截断标记、exit/status/duration；不保存 stdout 正文。

## 终态语义

`healthy` 必须同时满足：进程成功启动、exit 0、未超时/取消/截断、stdout 是唯一严格 Runtime Health JSON、Report
与 Launch Resolution 的 pointer/slot/version/target/boot/binary/path 完全一致、install-root exact 一致，且 report
timestamp 位于 launch-resolution 与 completion 之间。

其他所有已启动结果都持久化为 `unhealthy`，并提供机械 failure code：start failure、timeout、cancel、non-zero、truncated、
invalid report 或 binding mismatch。基础设施启动失败明确 `process_started=false`；其余终态明确 probe process 已启动。
所有 Receipt 固定 `user_session_started=false` 和 `opt_in_stage_completion_authority=false`。

动态 View 还会重验 source Deployment、5f2 active authority、历史 Launch Resolution 和 report binding。后续 rollback/pointer
推进不抹除历史 healthy Receipt，但会撤销当前 `runtime_health_authority`。

## 验收结果

- 8 个并发 observe 调用只执行一个真实 candidate health subprocess，并返回同一 terminal Receipt；
- 过期 lease 可被新 epoch 接管，旧 owner 提交被 fencing；
- 真实 candidate 通过生产 Runtime Health contract 反向验证 active chain/binary/environment；
- 父进程注入的测试 secret 未进入子进程环境；
- invalid JSON stdout 形成一个 durable unhealthy Receipt，reconcile 不重复执行；
- healthy 后 rollback 保留历史 Receipt，但动态撤销 active/runtime-health authority；
- Engine 与 lazy public exports 已装配；
- 2 个 5f3 真实场景测试、相关 launcher/runtime-health/Engine 小模块测试和 ruff 通过；未运行全量测试。

## 当前边界与下一步

- HAR-10.2i 已把真实 New UI/TUI runtime heartbeat 与 exact ARC-07.5e release identity 原子绑定，HAR-10.2j 又建立
  append-only、bounded page 的 release-bound observation ledger；但单次 probe 和样本集合都不能自行证明持续健康。
  下一切片应聚合有最小样本数、持续时间、最大 gap、连续 pointer/exposure 的 opt-in observation window，最后形成独立
  Stage Completion Evidence；legacy baseline 之前的未知历史不得计入；
- Windows 使用共同 Python 核心与环境白名单，但本轮真实 subprocess 夹具是 POSIX shebang，仍需 Windows runner 演练；
- percentage rollout 仍缺安装注册、稳定分桶、exposure accounting 与群体级 guardrail，不能由本机 Receipt 推导；
- unhealthy Receipt 只能成为后续 pause/rollback input，EVO-05.6b2 仍需消费 exact Rollback Request 执行兼容回滚。
