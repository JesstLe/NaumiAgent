# ARC-07.5e Managed Terminal Runtime Identity

## 目标

让由 stable launcher 启动的普通 New UI/TUI runtime 在进程内部形成 exact、content-addressed release identity，供
Harness heartbeat 绑定真实 pointer/slot/boot/binary。开发环境和源码运行保持明确的 `unmanaged`，不能伪造 release
identity，也不能因为缺少安装槽而阻断本地开发。

本切片只签发进程身份，不写 Harness、不证明 session 持续存活、不形成 rollout observation。

## 通用自校验内核

`verify_active_runtime_binding()` 是 Runtime Health 与 Terminal Identity 共用的唯一机械验证路径：

1. 重放 ARC-07 active pointer hash chain；
2. 验证 exact immutable slot、完整 manifest、Boot Receipt 与 runtime binary digest；
3. 要求 `NAUMI_ACTIVE_SLOT_ID`、`NAUMI_ACTIVE_POINTER_GENERATION`、`NAUMI_INSTALL_ROOT` 与 active chain 完全一致；
4. 按当前平台路径大小写规则要求执行路径等于 active runtime binary；
5. 返回只含已验证 target/path/install-root 的内存对象，不持久化环境或 secret。

Health probe 与 terminal session 使用同一事实内核，但签发不同语义 artifact：前者固定
`health_probe_process=true/user_session_started=false`；本 Identity 固定 `invocation_kind=terminal_session`、
`runtime_process_started=true`、`terminal_session_process=true`、`health_probe_process=false`，不能相互冒充。

## 发现与失败关闭

`discover_runtime_identity()` 的结果只有三种：

- 三个 launcher 绑定变量全部不存在：返回 `None`，明确表示源码/开发态 unmanaged runtime；
- 只存在部分变量：以稳定中文错误码失败关闭，不能降级成 unmanaged；
- 三个变量齐全：执行完整 self-verification，任何 chain/environment/binary 漂移都拒绝签发。

Identity 绑定 pointer、slot、version、target、Boot Receipt、binary digest、canonical runtime/install path 与验证时间，
并通过 SHA-256 形成 content-addressed ID。它不保存 argv、父进程环境、用户输入或凭据。

## 验收结果

- exact active runtime 签发 terminal-session identity，discover 与 direct inspect 结果一致；
- unmanaged 开发环境返回 `None`；
- partial managed environment 和错误 binary 均失败关闭；
- Runtime Health 继续复用同一验证内核且既有漂移语义不回退；
- identity/health 5 项小模块测试、公共导入和 ruff 通过；未运行全量测试。

## 下一步

HAR-10.2i 已把 Identity 与 runtime heartbeat subject/instance/epoch 原子绑定，并让 New UI/TUI 复用
Composition-owned factory；见 [HAR-10.2i](../harness/HAR-10-2i-runtime-release-identity-binding.md)。绑定失败只撤销
release identity authority，不阻断开发态 heartbeat/UI。

HAR-10.2j 已交付 append-only、bounded page 的 release-bound heartbeat observation ledger；见
[HAR-10.2j](../harness/HAR-10-2j-runtime-release-observation-ledger.md)。下一步仍需 EVO-05 聚合最小持续时间、样本数、
gap、pointer/exposure 连续性；identity binding 和 sample ledger 都不能直接形成 rollout window 或 Stage Completion Evidence。
[EVO-05.5f5e](../self-evolution/EVO-05-5f5e-percentage-runtime-exposure.md) 已先要求 exact percentage Deployment 与
startup→running sample pair 一致，形成单 installation Exposure Receipt；它仍不是持续健康窗口。
