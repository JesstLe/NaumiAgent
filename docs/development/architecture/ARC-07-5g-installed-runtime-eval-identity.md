# ARC-07.5g Installed Runtime Eval Identity

## 状态

已实现。

## 目标

补齐 exact installed backend Eval 的运行时身份事实，使后续 HAR-09.6c2 能复用 HAR-08 原生 H5c identity gate，
而不是把父进程平台或当前 workspace 版本冒充为已安装 binary 的身份。

ARC-07.5f 已证明“哪一个 slot/backend bytes 执行了哪些行为 case”；本切片进一步证明“该进程自报的 Naumi 版本、
操作系统与 CPU 架构是否和 slot manifest/target 一致”。它仍不生成 post-rollback H5c，也不授予 Outcome、learning
或 promotion authority。

## 身份合同

`ReleaseRuntimeEvalResponse.runtime_platform` 复用 `HarnessEvalPlatformIdentity`，由 exact installed binary 内部采集：

- `system`：`macos | linux | windows | unknown`；
- OS release 与 machine；
- Python implementation/version；
- 运行中 Naumi 包的 `__version__`。

该结构不读取或保存 hostname、用户名、HOME、环境变量、Provider、API Base、模型或凭据。字段有长度和枚举上限，
并进入 Response 与最终 Receipt 的内容摘要。

## 父进程交叉验证

Release Slot Store 在接受 Response 前机械执行：

1. `runtime_platform.naumi_version == slot.version`；
2. runtime `system + normalized machine` 必须等于 slot target；
3. slot target 仍必须等于当前 host target；
4. slot manifest/source、binary SHA-256、Request/Response digest 和执行时间窗口继续通过 ARC-07.5f 校验。

machine 仅接受 `arm64|aarch64 -> arm64` 与 `x86_64|amd64 -> x64`。未知架构、版本不符、平台不符全部以
`release_runtime_eval_identity_mismatch` 失败关闭，不能持久化 Receipt。

## H5c 解锁边界

HAR-09.6c2a 现已从四处组装完整且可复核的 Harness identity：

- Source：installed slot manifest 的 `source_commit/source_tree_sha256`；
- Configuration：原 H5c baseline 的 Suite/Profile/Policy/runner/repetitions；
- Platform：本切片由 exact binary 自报并经 slot target/version 复核的 runtime platform；
- Model：`protocol_hello@1` 保持 `null`，继续满足 `no_model` guardrail。

只有组装后的 identity 与原 H5c baseline compatible，才允许记录 fresh post-rollback samples/comparison。版本或平台
不兼容必须成为显式 unsupported/incompatible 结果，禁止降级为当前 workspace import。

## 验收证据

- 真实 POSIX installed bundle 执行六个 `protocol_hello` fixtures，Response/Receipt 版本等于 slot version；
- 伪造一个 digest 自洽但版本为 `9.9.9` 的 Response，Release Slot Store 在持久化前拒绝；
- hidden CLI 仍只输出 strict JSON，且 answer-stripped Process Request 不含 expected；
- 定向 Ruff、py_compile 与小模块 pytest 通过，不运行全量测试。

## 未完成边界

- Windows `.exe` 与 Linux frozen binary 的真实跨平台验收仍待平台 runner；
- runtime platform 只建立 H5c identity 前置，不代表行为恢复；
- HAR-09.6c2a 已绑定 6c1 Verification、Outcome、原 H5c baseline cohort、fresh installed-runtime samples 与
  fresh H5c comparison；完整跨平台矩阵仍等待 HAR-09.6c2b；
- 长期观察窗口和 learning/promotion authority 继续关闭。
