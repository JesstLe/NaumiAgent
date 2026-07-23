# HAR-08.4i Native Sandbox Eval Service

## 目标

把 HAR-08.4h Request、HAR-08.4g admission、HAR-08.4f Batch coordinator、HAR-08.4e execution
kernel 与既有 H5a Store 组合成唯一共享 Service 执行入口。Tool、Slash、New UI 与 TUI 后续只能调用该入口，
不能自行签发 grant、循环 samples 或拼装 Eval 结果。

## 共享执行链

`HarnessService.eval_sandbox()` 现在负责：

1. 要求 Runtime 已组合 `HarnessSandboxEvalExecutor`，且当前调用存在持久权限回执；
2. 读取当前 Profile 与 Trust Store，拒绝 missing、invalid 或 untrusted Profile；
3. 在线程中调用 HAR-08.4h builder，从干净 Git revision、ordered checks、samples、batch 和预算编译
   immutable request；
4. 把 request 与父权限交给共享 executor；
5. executor 机械要求父回执的 tool 为 `harness_eval_sandbox`，参数摘要精确匹配
   `check_ids + samples + batch_id`，并已委托 `bash_run`；
6. 每次恢复、执行前和持久化前重新读取 Profile，要求 digest、trust 与每项 check spec 都没有漂移；
7. coordinator 取得 HAR-08.4g 容量槽、独占 Runtime lease 和一个 batch-scoped Run Grant；
8. execution kernel 从 request 的精确 Git revision materialize 每个 sample，所有 checks 通过 ARC-04 Worker
   执行；
9. 每个 sample 立即写入既有 H5a `harness_eval_results`，中断后只从连续且权威匹配的前缀继续；
10. 完成后撤销 Run Grant、释放 lease，并签发稳定的 `hsevalbatch_*` receipt。

Engine 在启动时只组合一份 admission、native kernel 和 executor；Evolution 仍使用相同底层 kernel 与 admission，
但保留自身 request/receipt 语义。

## H5a 与结果身份

每个 native sample 使用 `harness_sandbox_*` Suite identity，并绑定：

- 精确 source commit 与 raw tree SHA-256；
- Profile SHA-256；
- ordered check spec/argv/timeout；
- runner、comparison policy、sample 数和平台身份；
- 每项 ARC-04 lifecycle receipt 与 batch Run Grant digest。

恢复时不仅检查 batch/suite/sample index，还会复验完整 baseline identity、Suite digest、case 顺序、runner、
check status 到 Eval status 的映射、ARC-04 lifecycle marker 和 `run_scope=batch`。结构相似但 authority
不一致的 H5a 记录会在 admission、lease、grant 和项目代码执行前 fail closed。

完整 Batch receipt 固定 request、Suite、batch、全部 sample result digest、唯一 identity、可能跨恢复产生的
有序 Run Grant digests、checks 和完成时间。完成时间取最后一条已持久化 H5a 时间，因此相同完整 Batch 的重复
调用返回相同 receipt，不会因为查看时间不同而漂移。

## 验收证据

- 使用真实临时 Git 仓库、真实 Profile/Trust Store、Permission Store、Run Grant Store 与 H5a SQLite；
- 5 个 samples 完整执行后形成 5 条连续 H5a，Run Grant 已撤销，重复调用不再次执行并返回相同 receipt；
- sample 2 中断时保留 2/5 前缀，重试使用新 Run Grant 从 sample 2 继续到 5/5，receipt 记录两个 grant digest；
- 与 `check_ids + samples + batch_id` 不精确匹配的父权限在 lease/grant/执行前拒绝；
- 外来或伪造语义的 H5a 前缀在 grant/执行前拒绝；
- kernel 返回的 check 顺序、Profile/source identity 或 ARC-04 evidence 不完整时，在 H5a 写入前拒绝并清理
  Run Grant/lease；
- Engine production composition、既有单检查路径和 4e/4f/4g 小模块回归通过；
- 未运行全量测试。

## 当前边界与下一步

HAR-08.4i 建立的是生产 Runtime 内部共享执行面，尚未注册 `harness_eval_sandbox` Tool，因此当前用户还不能从
Slash/New UI/TUI 启动这条链路。测试中的 execution kernel 使用受控替身验证 Service 编排；真实 ARC-04
materialization/Worker 命令执行由既有 HAR-08.4e 与单检查真实后端测试覆盖，本切片没有重复实现 Worker。

下一切片应注册唯一 `harness_eval_sandbox` Tool 与 permission rule，并让 `/harness eval sandbox` 通过
`engine.execute_tool()` 获得精确父回执；随后新增共享 typed renderer，再接 New UI/TUI progress checkpoint。
跨进程 admission、跨主机 dispatcher 和 Linux/Windows 真实隔离 CI 仍不属于本切片。
