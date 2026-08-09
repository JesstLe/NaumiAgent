# ARC-07.5b Stable Active-Slot Launcher

## 目标

让用户 PATH 中的 `naumi` 成为版本槽之外的稳定 launcher。每次启动都从 ARC-07.5a 的 SQLite
active pointer 解析、验证并启动 exact `naumi-runtime[.exe]`，使安装升级、人工回滚和后续
EVO-05.6b2 自动回滚实际作用于用户进程，而不只是写入一条数据库记录。

本切片不实现远端 channel、签名、数据迁移或自进化回滚策略；它只提供这些模块共同依赖的本地启动权威。

## 发行与安装契约

- PyInstaller 分别构建 onedir 稳定 `launcher/naumi[.exe]` 和完整 `naumi-runtime[.exe]`；Terminal UI 仍为
  `naumi-ui[.exe]`。launcher 不使用 one-file 临时解压；macOS arm64 冻结产物自检实测约 0.17 秒，原 one-file
  方案约 7.6 秒，已拒绝采用。
- bundle manifest 必须同时包含 launcher、runtime、Terminal UI 和配置示例，并逐文件绑定 SHA-256。
- macOS/Linux/Windows 安装器先验证 archive checksum 与安全路径，再自检 bundle 内 launcher。
- launcher 安装到 `<install-root>/launchers/<bundle-version>` 的只读版本目录；PATH 只原子链接或 shim 到该
  launcher，不再指向 runtime slot。升级失败时旧 PATH 入口保持不变。
- `--launcher-install <bundle>` 依次执行 immutable slot install、真实 runtime `--version` boot probe 和
  atomic active pointer switch。失败时 PATH 保持指向旧 launcher，且不生成半个 active generation。
- 同一 bundle 重复安装幂等复用同一 content-addressed slot。

## 启动解析与失败关闭

普通启动和 `--launcher-status` 都必须：

1. 在一致 SQLite read transaction 中重放完整 activation hash chain；
2. 验证 pointer、slot、host target、完整 manifest、只读属性和 current Boot Receipt；
3. 重新计算 runtime digest，拒绝删除、增加、修改或 mutable slot；
4. 生成 content-addressed Launch Resolution，再在 `BEGIN IMMEDIATE` 中确认 pointer 未变化并持久化；
5. pointer 在解析与写入间变化时最多重新解析三次，不能启动 stale slot；
6. POSIX 使用 `execve` 保持信号/退出语义，Windows 子进程完整透传参数、环境和退出码。

冻结 runtime 还提供隐藏的机器接口 `--runtime-health-check`。它不进入 onboarding/UI，而是在进程内部重新读取
`NAUMI_INSTALL_ROOT`，重放 active chain，验证 manifest、Boot Receipt、runtime binary path/digest，并要求 launcher
注入的 slot ID 与 pointer generation 完全一致。成功只输出一个不超过 64 KiB 的 content-addressed JSON Report；
额外日志、环境漂移、非 active binary 或损坏链全部失败关闭。Report 明确区分 health probe 进程已启动与用户 session
未启动，供 EVO-05 opt-in runtime observation 使用。

Launch Resolution 只保存参数数量，不保存用户参数正文。`--launcher-status` 明确记录
`process_start_requested=false` 和 `process_start_authority=false`；只有普通启动拥有 process-start authority。
解析失败、链损坏、slot 篡改或 Boot Receipt 过期时输出中文错误码并以 78 退出，不静默猜测或退回未知版本。

## 验收结果

- 真实 POSIX 安装夹具完成 archive 下载、checksum、launcher 自检、slot install、boot、activate 和参数转发；
- 重复安装成功且只产生一个 immutable slot；
- status 解析 active slot 但不授予进程启动权限，数据库不持久化原始参数；
- runtime health machine interface 反向验证 exact active binary/environment，并严格解析单一 JSON Report；
- 篡改 active runtime 后 launcher 失败关闭；
- artifact/slot/launcher/installer 相关 28 项小模块测试通过；
- ruff、shell syntax 和 diff whitespace 检查通过；未运行全量测试。

## 当前边界与下一步

- archive 目前仍只有 SHA-256；[ARC-07.4a](ARC-07-4a-exact-source-provenance.md) 已绑定 exact source
  commit/tree，但 ARC-07.4 仍必须增加可信签名、SBOM 和平台 notarization；
- Windows 脚本完成静态契约与共同 Python 核心覆盖，但仍需 Windows runner 的真实安装/进程演练；
- ARC-07.6 必须在涉及 config/schema/data 的升级前建立 snapshot 与兼容性 gate；
- 下一最小闭环切片是 EVO-05.5f3：消费 exact Deployment Receipt，通过 stable launcher 解析并启动冻结
  runtime 的 health machine interface，持久化带执行终态的健康观测回执；它不启动用户 session，也不据此宣称
  percentage/stable rollout。EVO-05.6b2 再消费失败观测形成的 exact Rollback Request。
