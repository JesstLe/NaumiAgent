# EVO-02.7b1 Mutation Generation Trace 与虚拟文件工具 v1

## 目标

让自进化模型生成 proposed contents 时留下真实、顺序化、可机械复核的工具轨迹，同时确保生成阶段
不会提前写入实验 worktree。模型只能调用 Mutation Plan mutation stage 已授权的 `file_edit` 和
`file_write`；工具在内存草稿上执行，成功结果随后才交给 Static Guard 与 Patch Writer。

本切片解决“内容是怎样生成的”这一事实缺口。EVO-02.7b2 已在后续切片把 Trace 强绑定到 Guard v2、
Writer v3 与 Mutation Receipt v2，详见 `EVO-02-7b2-trace-receipt-binding.md`。

## 为什么不能直接复用普通 Run Receipt

现有 Harness `EvidenceCollector` 已正确保存 tool name、call-id digest、参数/result digest、权限和耗时，
但出于通用隐私边界不保留 mutation path、逐次 before/after digest 或 edit 顺序。普通 Completion
Receipt 只在运行结束后归因最终 Git change，也无法证明：

- `file_edit` 的 old text 是否唯一命中；
- 多次 edit/write 的中间状态和顺序；
- failed call 后是否进行了合法重试；
- 最终 proposed bytes 是否就是某个成功 tool call 的产物；
- 生成阶段是否在 Guard 之前触碰磁盘。

因此本切片没有扩张通用 Harness Evidence schema，而是建立仅服务受控自进化的窄化执行边界。

## Authority 与真实 baseline

`EvolutionMutationGenerationService.begin()` 重新验证 Contract、active Lease、Source Snapshot 和
Mutation Plan 的完整 Pydantic payload，并机械复核：

- Contract manifest、baseline commit、Lease/Snapshot/Plan identity；
- Candidate ID/revision/digest 与 approved Contract；
- Plan scope 等于 Contract allowed files；
- mutation stage 只允许 `file_edit/file_write`；
- attempt 位于 Plan 上限内，且同 Plan attempt 尚无不可变 Trace。

Service 从 Lease worktree 读取每个真实 baseline 文件。读取使用 no-follow file descriptor、inode
前后校验、2 MiB 上限和 UTF-8 校验；摘要必须等于 Mutation Plan。create 目标当前存在时 fail-closed。

## 虚拟工具执行

`EvolutionMutationGenerationSession.execute(ToolCall)` 在单一 `asyncio.Lock` 内串行执行：

- `file_write(path, content)`：在内存草稿中创建或覆盖完整 UTF-8 内容；
- `file_edit(path, old_text, new_text)`：要求目标存在且 old text 精确出现一次，再执行一次替换；
- path 必须属于完整 approved scope；绝对路径、`..`、控制字符和额外参数被拒绝；
- content、old/new text 与 canonical arguments 有严格大小上限；
- 未授权工具、同 call-id 更换参数、超 Plan tool budget 使 session 进入不可恢复失败；
- 同 call-id、同原始参数 replay 返回缓存结果，不重复执行、不重复计数；
- 合法 edit 失败会形成 error fact，可在预算内用新的 call-id 重试。

工具返回值只说明内存草稿是否更新，不回显源码。Session 不调用通用 `FileWriteTool/FileEditTool`，因此
生成阶段主工作树与 Lease worktree 都保持原字节和 clean。

## Trace artifact

`EvolutionMutationGenerationTrace` 包含：

- run、Contract、Lease、Snapshot、Plan、attempt 与 tool budget；
- 每个 call 的连续 order、hashed call-id、tool/path、status/error code；
- canonical arguments digest/size、result digest/size、before/after digest；
- 每个最终文件的 baseline/final digest、size 与最后成功 call order；
- calls/final-files 集合摘要、成功/失败/总调用计数；
- `trace_ready=true`、`write_authorized=false`、`execution_ready=false`。

模型校验器重建完整 per-path digest chain：失败 call 不得改变状态，每个 call 的 before 必须等于前一
状态，最后成功 call 必须等于 final file。Trace 不保存源码、old/new text、原始 call-id、tool result
正文或绝对 worktree path。

## 持久化与并发

`EvolutionMutationGenerationTraceStore` 在 runtime SQLite 中以 `(mutation_plan_id, attempt)` 唯一保存：

- canonical Trace JSON 不超过 256 KiB；
- 同一 Trace 并发 put 收敛为同一行；
- 同 Plan attempt 的不同 Trace 返回 `mutation_trace_conflict`，禁止覆盖；
- 读取时重新验证嵌套 fact、digest chain、Trace identity 和索引列；
- 已有 Trace 的 attempt 不能开始第二个 generation session。

`AgentEngine` 组合共享 Trace Store 与 Generation Service，但尚未注册普通 Slash/Agent Tool。后续专用
Evolution runner 应直接把模型产生的原始 `ToolCall` 交给 Session，而不是经过可写磁盘的通用工具。

## 验收证据

- 真实单文件 Git baseline 上执行虚拟 `file_edit`，并发相同 call replay 只记录一次；
- finalize 产出的 proposed contents 可直接通过 Static Guard 并由 Patch Writer 写入隔离 worktree；
- 生成期间主工作树与 Lease worktree 字节、Git status 均不变；
- failed edit + successful retry 形成连续 digest chain；
- 真实双文件 Plan 用两次虚拟 `file_write` 完整覆盖 scope，最终文件按路径排序；
- scope 未覆盖、path escape、未知工具、call-id collision、预算溢出均 typed fail-closed；
- Trace 嵌套 fact 与 SQLite JSON 篡改被拒绝；Trace Store 并发同内容只形成一行；
- `AgentEngine` composition 与 EVO-02.7a 聚焦回归保持通过。

## 后续实现状态与当前不足

- Trace 与 proposed contents 由同一个内存 Session 同时返回；EVO-02.7b2 已让新 Mutation Receipt v2 引用
  `trace_id/trace_sha256`，并机械核对 final file digest；
- proposed contents 为短生命周期内存数据，故意不持久化；若 Trace committed 后、Writer 前进程崩溃，
  必须以新 attempt 重新生成，不能从摘要逆推出源码；
- protocol-fatal session 当前不会持久化“失败 Trace”，只通过 typed error 终止；后续审计事件可记录
  failure code，但不得保存原始参数；
- 专用模型 turn runner、取消/超时与有限 Runtime Event 已由 EVO-02.7c1 实现；protocol/cancel/timeout
  failure 仍没有不可变 generation audit artifact；
- Trace committed 后、Writer 前的源码草稿仍不持久化，进程崩溃必须用下一 attempt 重新生成；
- HAR-08 RED/GREEN 尚未消费 Mutation Receipt v2，因此 EVO-02 仍未满足完整阶段门。
