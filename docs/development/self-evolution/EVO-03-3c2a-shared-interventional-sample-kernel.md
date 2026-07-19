# EVO-03.3c2a Shared Interventional Sample Kernel

## 目标

把 EVO-03.2e/2f 的 RED 单样本执行治理抽成 RED/GREEN 共用内核，同时保留双方独立的 authority、source、
identity、metric、suite 和 receipt 语义。该切片先让现有 RED 全量迁移并回归，不提前宣称 GREEN sample 已完成。

## 共用职责

`EvolutionInterventionalSampleKernel` 唯一负责：

- 重新读取并验证当前 Harness Profile/check identity；
- 校验父权限与 `bash_run` 委托范围；
- 单 sample Runtime lease 的取得、Run Grant 签发/消费、失败清理与终态撤销；
- cohort 外部 Run Authority 的 fence/parent/run/grant digest 复验；
- 对每个有序 check 调用同一个 ARC-04 Sandbox Runner 与 Shell admission composer；
- 要求每项结果具有 ToolJob/lifecycle receipt；
- 在 suite 构造后、首次 H5a 持久化前再次调用可选 source-current callback；
- existing sample 仲裁、不可变 H5a 写入及 owned authority 清理。

内核不决定 RED/GREEN 的 source bytes、metric 数值、Eval identity、结果标题或 receipt schema；调用方通过
typed source、existing validator 和 async suite builder 注入这些差异。

## 兼容性

RED executor 已删除自身的 lease/grant/admission/cleanup 实现并完全委托内核。RED 的 owner ID、Run Grant
idempotency key、sample run ID、H5a identity 与 receipt 保持原派生规则，避免升级破坏在途恢复。

## 验收标准

- standalone RED sample 仍取得/撤销自己的 Runtime lease 与 Run Grant；
- cohort RED samples 仍消费外部 authority，且不由 sample 撤销；
- 精确 revision Profile check、metric 合并、H5a 幂等、Profile drift 和 authority tamper 回归通过；
- kernel source callback 在 suite 后、H5a 前可阻断 candidate 漂移；
- Ruff 与 focused tests 通过，不运行全量测试。

## 下一步

EVO-03.3c2b 使用同一 kernel：GREEN source 由 HAR-08.4d baseline+overlays 提供，metric 扫描读取
EVO-03.3c1 immutable blobs，identity 复用 RED configuration/platform 并把 candidate fingerprint 标为 dirty，
最后生成独立 GREEN sample receipt。
