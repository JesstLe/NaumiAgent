# EVO-02.7b2 Generation Trace 到 Mutation Receipt v2 强绑定

## 目标

把 EVO-02.7b1 的不可变 Mutation Generation Trace 变成自进化验证链的强制权威输入，关闭
`模型工具调用 -> proposed contents -> Static Guard -> Patch Writer -> Mutation Receipt` 之间的证据断点。

本切片不负责调用模型；它负责确保任何准备进入 EVO-03 验证的 Mutation Receipt 都能机械证明：被写入的
完整文件字节来自同一个 Plan attempt 的虚拟工具 Trace，而不是由调用方在 Guard 或 Writer 之间替换。

## 版本边界

为保留历史 artifact 的读取与恢复能力，本切片采用显式版本升级：

- `EvolutionStaticGuardReceipt v2`：新增 Trace ID、Trace digest 与 generation attempt；
- `EvolutionPatchWriteReceipt v3` / `EvolutionPatchSetWriteReceipt v3`：新增同一组 Trace binding；
- `EvolutionMutationReceipt v2`：新增 Trace identity，并把 generation artifact 加入第一项 tool evidence；
- 历史 Guard v1、Writer v1/v2、Mutation Receipt v1 继续可解析；
- 历史 Writer 回执不能再生成新的 validation-ready Mutation Receipt，防止兼容路径绕过生成证据。

Static Guard 的内容安全策略仍是 `evolution-static-guard-v1`；Receipt schema 升级只增加来源绑定，不伪装成
新的内容扫描策略。

## Static Guard v2

`EvolutionStaticGuard.preflight(..., generation_trace=...)` 会完整重验 Trace Pydantic payload，并核对：

- Contract manifest、Lease、Source Snapshot、Mutation Plan ID 与 digest；
- Trace `max_attempts` 等于 Plan；
- Trace final file path 集合等于 proposed contents；
- 每个 proposed file 的真实 UTF-8 bytes digest 等于 Trace final digest；
- Trace 仍为 `write_authorized=false`、`execution_ready=false`。

只有传入 Trace 才生成 Guard Receipt v2。未传 Trace 的调用仍生成历史 v1 artifact，供独立策略诊断和历史
恢复测试使用，但 Writer v3/Mutation Receipt v2 不接受它。

## Writer v3 与 attempt 门禁

单文件和多文件 Writer 在收到 Guard v2 时必须同时收到完整 Generation Trace：

- 漏传 Trace 返回 `generation_trace_required`；
- Guard 引用与 Trace 不同返回 `generation_trace_guard_mismatch`；
- Writer 持锁后重跑带同一 Trace 的 Static Guard，防止 proposed contents 被替换；
- Journal/Patch Set `prepare` 后、任何文件替换前，机械比较事务 attempt 与 Trace attempt；
- attempt 不一致时不写文件，事务进入 rolled-back 状态，并消耗对应失败 attempt；
- 成功 Writer v3 回执持久化 Trace ID、digest 与 attempt；committed replay 再次核对全部 binding。

旧 Guard v1 + 无 Trace 仍可重放历史 Writer v2 行为，以便已有崩溃恢复事实可读；该结果被 Mutation Receipt
Service 视为 legacy，不能进入新的验证链。

## Mutation Receipt v2

`EvolutionMutationReceiptService.finalize()` 现在强制接收完整 Generation Trace，并重新验证：

- Trace、Guard v2、Writer v3、Contract/Lease/Snapshot/Plan identity 全部一致；
- Trace final files 与 Guard changes 的 path、operation、before/after digest 全集一致；
- committed Journal/Patch Set attempt、Writer Trace attempt、Generation Trace attempt 三者相等；
- Writer 回执仍包含与当前 worktree 相等的完整 Postflight Guard；
- 旧 Writer v1/v2 返回 `mutation_write_receipt_legacy`，不能生成 validation-ready Receipt。

Mutation Receipt v2 的 tool evidence 固定为四段顺序：

1. `mutation_generation`；
2. `static_guard`；
3. `patch_write`；
4. `postflight_guard`。

Receipt 不保存源码、diff 正文、原始 call ID、绝对 worktree 路径或模型参数。

## Attempt 恢复语义

Generation Trace 以 `(mutation_plan_id, attempt)` 不可变保存。若进程在 Trace finalize 后、Writer commit 前
退出，proposed contents 不可由摘要恢复，必须使用下一 attempt 重新生成。Writer 不允许把 attempt 2 Trace
写进 attempt 1 Journal/Patch Set；错误 attempt 会在落盘前失败，随后由相同 Trace 在事务 attempt 2 重试。

这一约束避免“重新生成了内容，却沿用旧 attempt 的治理与评测结果”。

## 验收证据

- 真实单文件 Git worktree 完成 Trace -> Guard v2 -> Writer v3 -> Mutation Receipt v2；
- 真实双文件 write-set 完成相同链路，Receipt final digest 与 Trace 全集相等；
- proposed bytes 与 Trace 不同，在 Static Guard 前 fail-closed；
- Guard v2 漏传 Trace、同 call scope 的不同 Trace 均被 Writer 拒绝；
- attempt 2 Trace 首次遇到 attempt 1 Journal 时不落盘并回滚，第二次在 attempt 2 成功提交；
- Mutation Receipt tool evidence 固定为四段且 Trace 字段篡改被拒绝；
- 历史 Mutation Receipt v1、Writer v1/v2 仍可解析，但历史 Writer 不能进入新验证链；
- 原 Generation Trace、Receipt、单/多文件 Writer 聚焦回归保持通过。

## 当前不足与下一步

- 尚未实现专用 model turn runner；目前调用方需要把模型原始 `ToolCall` 逐个交给 Generation Session；
- protocol-fatal generation session 仍只有 typed error，没有不可变失败审计 artifact；
- 尚未执行 HAR-08 RED/GREEN、基线对照和指标归因，因此 EVO-02 仍为 partial；
- 下一最小切片应跨查 Harness 与 EVO-03，优先实现受 Harness 取消/超时/事件协议约束的专用 Mutation Turn
  Runner，再把 Mutation Receipt v2 交给 EVO-03 Validation Plan，不能直接扩张自修改范围。
