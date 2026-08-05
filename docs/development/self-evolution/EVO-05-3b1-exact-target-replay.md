# EVO-05.3b1 Exact-target Isolated Replay

## 1. 目标

把 `ready` Revalidation Request 从“可执行输入”推进为真实源码动作：从 active Experiment Lease 逐文件读取
Candidate bytes，重新核对批准的 Patch Manifest，再在一次性 detached Git worktree 中写入、复核并产生 durable
Replay Receipt。

本切片只处理 `target == baseline` 的 `validate_exact_tree`。目标已经前进时必须失败关闭，不能把覆盖文件伪装成
rebase。它不运行验证命令，也不产生 merge、push、publish 或 promotion authority。

## 2. 权威链与真实数据

`EvolutionRevalidationReplayService` 每次执行都重读 Request current view、exact Promotion Package Input、Reflection
eligibility 和 Input 所绑定的 Experiment Lease。Request、Input、Patch Manifest、Experiment Contract、Lease、baseline
和 target 必须逐项一致。Lease 必须 active、未过期，路径必须是受管 worktree 存储目录的直接子目录。

Executor 从真实 Git/source 文件重算 before/after SHA-256、与 Postflight Guard 相同格式的 unified diff SHA-256、普通
文件类型、2 MiB 上限和 executable mode。Candidate source HEAD 必须仍等于 baseline，status 只能包含 Manifest 声明的
未暂存 modify/create 文件。任一差异均 fail closed，错误不包含源码或 diff 内容。

## 3. 隔离执行

Executor 使用 `git worktree add --detach <path> <exact-target-head>`：

- 不 checkout 或移动 target branch，不写主工作区或 Candidate source；
- modify 只有 target blob 等于批准 before digest 才能写；create 只有目标不存在才能创建；
- 使用同目录临时文件、`fsync`、mode 设置和 `os.replace` 原子落盘；
- 写后重读 bytes、diff 和 Git status，禁止批准范围外变化；
- 成功失败都移除 detached worktree 并 prune metadata。

成功前后还会比较主工作区、Candidate source 和 target branch 的 HEAD/status。并发漂移会拒绝签发 Receipt。

## 4. Durable Receipt 与幂等性

`EvolutionRevalidationReplayReceipt` 使用 `evolution-revalidation-replay-v1`，保存 exact Request/Input/Lease、target、
逐文件 baseline/candidate/replay/diff digest、mode、replay tree digest 和隔离/清理证明。network、dependency install、
validation executed 和 promotion authority 固定为 false。

SQLite `evolution_revalidation_replays` 对 `request_id` 唯一。进程内同一 Request 使用 singleflight lock；重复调用返回
同一 immutable Receipt，Store 拒绝同一 Request 的不同结果。

## 5. 双通道入口

```text
/evolution revalidation-replay <revalidation-request-id>
```

Agent Tool 为 `evolution_revalidation_replay`。Slash、Tool 和 Engine 都调用同一个 Service。工具为中风险、非
lockdown 可调用、每会话最多 50 次；bypass 不做二次确认，但不能跳过 authority、digest 或隔离不变量。

## 6. 验收证据

- 真实临时 Git repository、source worktree 和 detached replay worktree；
- Candidate bytes 实际写入 replay 后逐字节复核；
- 六路并发调用得到同一 durable Receipt；
- 主工作区保持 baseline，Candidate source 保持变异，target branch 不移动，临时 worktree 已删除；
- stale Request、advanced target 和 Candidate source drift 全部 fail closed；
- Slash、Agent Tool、PermissionRule、lazy export 和 Engine composition 已接通；
- 仅运行 Replay/Request 小模块测试，未运行全量测试。

## 7. 自我审视与剩余工作

本切片第一次让 EVO-05 审批链产生真实隔离源码写入，不再停留在哈希或自然语言回执。但完整闭环仍缺：

- EVO-05.3b2：目标前进后的三方 rebase、冲突 artifact、跨进程 fencing 与崩溃残留恢复；
- EVO-05.3c：为 replay tree 重绑 Validation Plan，运行 Harness checks/Eval cohorts 并签发新 receipts；
- [EVO-05.3d](EVO-05-3d-revalidation-outcome.md)：已形成 durable Outcome，并机械失效旧 promotion evidence；
- EVO-05.4-05.7：rollout、监控、自动回滚和最终 Outcome/Feedback。

当前 Receipt 只证明“批准源码可安全重放”，不能证明 Candidate 正确，更不能授权 Promotion。
