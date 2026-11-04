# PA-04：持久预算与逐模型请求许可

日期：2026-11-05

## 目标与费用口径

本模块实现**按已核验单价计算的本地估算消耗上限**，不是 provider 最终结算账单的保证。
每次请求都在原 Item 执行器使用的同一个 ModelRouter 调用之前持久预留，在响应后凭完整 transport
用量结算；不增加第二套模型执行路径，不补跑历史 Run 获得用量，也不把缺失字段变成零。

## 预算事实

预算通过 `RoutineBudgetMixin` 暴露与纯函数相同的接口：

```python
budget_initialize(tx, routine)
budget_reserve(tx, routine, occurrence_id)
budget_view(tx, routine)
budget_started(tx, occurrence)
budget_settle(tx, occurrence, known_cost_microusd, evidence, started)
```

父记录 kind 为 `budget`，id 为 `budget:<routine_id>`，包含：

- `reserved_cost_microusd / settled_cost_microusd`；
- `reserved_executions / started_executions`；
- `unknown_reservations`、估算策略版本与状态。

每个 occurrence 有单独 `reservation:<occurrence_id>` 记录，包含原预留金额、启动标记、结算状态、
确切证据 SHA、逐调用占用/结算金额、pending/unknown 调用数和下一个调用序号。不会把百万次执行塞进
单个 2 MB JSON 对象。所有修改保留 RoutineStore 的不可变历史。

`routine.definition.limits` 的总执行次数和总费用覆盖整个生命周期；修改配置 revision 或 schedule epoch
不重新初始化预算。把新上限降到低于已结算+原预留、已启动+原执行名额的操作会被拒绝。

一次预留金额为 `min(有限单次上限, 总剩余额度)`。执行次数在预留时占名额，实际启动时只移动至
`started_executions`；失败、取消、超时和 UNKNOWN 的已启动执行仍占一次。相同 occurrence 的预留/启动/
结算重试不会再次扣减，原 `request_id` 还能在 store 层原子地绑定预算、cursor、occurrence 与回执。

## 未知与零消耗的区别

- 缺失 Run 用量、缺失 transport usage、部分流、断线、取消、原费用冲突：保留全额预留和 UNKNOWN。
- 尚有 pending/unknown call 时，即使 Run 默认计数为零也不释放 occurrence 预留。
- 费用超过原估算预留时保留真实报告金额作为 overrun 证据，冻结新的预算受理，不伪造一个被截成上限的
  “已知费用”。原额度继续作为未结 hold；界面不能把它表述成已对账的完整费用。
- 已知最终费用使用 `max(原 Run 费用, 逐调用已结算费用)`，防止 Run 汇总的舍入抹掉逐调用向上取整。
- 真实零值只有完整响应的明确 transport usage 证据才作为零费用结算。

未启动释放有两类机械证据：

1. 从原 JobStore 核验得到的 lifecycle receipt，reason 为 `agent_job_cancelled_before_claim` 或
   `agent_job_cancelled_before_start`，绑定原 Job、request SHA 与 receipt SHA。
2. `proof_kind=routine_pre_dispatch`：预算函数自己在当前事务读取原 occurrence，核对
   `item_create_attempted is False`、`item_id is None`、状态为 `skipped/blocked`，以及
   `evidence.decision_sha256 == row.decision_digest == sha256(canonical(row.decision))`。
   原 occurrence 历史只要有过 attempted=true 或实际 Item ID，就不采用此释放证明。

仅仅缺少 Job 行、用户声称未执行、调用者传一个真假标记，都不构成零消耗证明。
dispatch 决定、cursor 推进与此类释放可以在同一事务完成；并发 dispatch 会在同一 SQLite 仲裁下留住
真实已尝试分支的 hold，而不是由最后写入者把它当零。

## 每次 transport 的持久许可

```python
permit = await service.reserve_transport_call(intent, router, args, kwargs)
await service.complete_transport_call(permit, usage, evidence)
await service.unknown_transport_call(permit, evidence)
```

普通 Item 返回 `None`，不建立周期预算或调用记录。周期 Item 在授权 gate 核验后创建 kind=`call` 的
独立事实，id 包含 occurrence 和单调 sequence。请求正文只保存 SHA/字节数，不持久化消息正文或凭据。
许可保存原 request SHA、模型、单价来源、输入估算上界、max_tokens、预留费用与估算策略版本。

请求许可步骤：

1. 从 ModelRouter capability contract 取得实际模型与逐字段单价，只接受 `catalog/config/litellm`。
   `fallback/unavailable` 不启动请求；NaN、负数、无效价格不变成免费。
2. 对确切消息和工具等输入进行 UTF-8 序列化，以字节量加显式协议余量构造本地 token 估算上界。
   无法从当前输入界定的远程多模态内容不使用该文本费用许可。
3. 取有限请求输出上限与模型上限的最小值，根据本次剩余费用继续收紧 `max_tokens`。
4. 在同一事务持久 call 与费用占用，然后才调用真实 router；预算 hook 失败时调用次数为零。
5. 同 occurrence 已有 pending/unknown call 时不再签发下一个许可。进程重启也不会把旧未知调用当成
   未发送重试。新的正常 retry/compaction 请求各用独立 sequence，并走同一 wrapper。

调用实际 token 或报告费用超过许可时，不把它截断成可接受值；call 保留 UNKNOWN 与原 hold。
许可与持久原件的模型、金额、输入 SHA、序号、输出上限等必须一致，调用者修改 permit 不能释放预算。

## Item 执行接缝与非流式假零修复

`PersonalItemExecutionContext.observe_model_usage()` 仍包装原 child router 的 call/stream。
所有 compaction、恢复与普通调用使用同一包装，不替换 engine 或创建新 Job。

- 非流式明确核验 `response.call_evidence.usage_source == transport_response`，并要求可用费用来源和
  完整终态；不再把默认 `ModelResponse.usage=TokenUsage()` 当作真实零值。
- 上述来源还不足以覆盖 `usage={}`：原 Router builder 会把缺失 prompt/completion 字段补成零。
  上下文拦截原 `_build_usage`，把每个生成的 TokenUsage 对象与原始两项计数的完整性关联；任何一项
  缺失都保持 UNKNOWN。显式 `prompt_tokens=0 / completion_tokens=0` 才是可验证的真实零。
- 流式要求实际 usage 段、终态标志与迭代器正常耗尽三者都存在。得到 usage 后又断线、取消或提前关闭
  迭代器时仍是 UNKNOWN，不因见过一个 usage 对象而结算。
- 用量字段校验拒绝负数、非整数 token、NaN/负费用。
- 不完整调用使运行上下文停止后续模型/工具边界，engine 的恢复逻辑也不会再次发出同一个未知请求。
- 预算预留可能等待持久 writer；等待返回后重新核验原 Item/Job fence，避免等待期间的暂停、取消或
  失租被先前的授权检查掩盖。此 gate 变化时实际 transport 调用次数为零。
- 执行器原有 Job/Run/receipt、UNKNOWN 不重放、publication 和人工已读协议不变；旧记录不迁移或重写。

## 验证范围

新增 `test_personal_routine_budget.py` 与扩展的 `test_personal_item_execution.py` 集中验证实际 SQLite
预留/结算、冷重启、并发最后名额、每次模型许可、输入/价格上界、输出收紧、真实零与默认假零、部分流、
取消、回执写失败、pre-dispatch 证明及篡改/并发。transport 均为受控零付费函数。

最终集中命令：

```powershell
.venv/Scripts/python.exe -m pytest tests/unit/test_personal_routine_budget.py tests/unit/test_personal_item_execution.py -q --disable-warnings --tb=short
```

最终观察：`79 passed in 39.81s`，exit 0，无 skip。日志为事务目录的
`routine-budget-item-tests-final2.txt`。认领四个 Python source/test 文件的 ruff check 为
`All checks passed!`，exit 0。先前 69 项结果早于原始字段补零检查；原始字段修复后同组先观察到
79 项通过。随后 runtime 接通了真实 Routine service，独立 ItemStore 组件 fixture 因误连 engine 的
另一份 Item 库出现 11 failed / 68 passed；生产逻辑没有增加“找不到 Item 就跳过周期授权”的例外。
fixture 现在明确隔离该组件未使用的预算 port，真实 hook 在新增上下文和 RoutineBudget 测试中覆盖；
上述最终结果包含该修正及逐 call 超额冻结父预算的验证。失败日志保留，不改写为通过。

另通过 `routine-usage-baseline-modified.py` 加载封存原执行器源码、当前源码与独立 rollback 副本，
实际调用原 Router `_build_usage`，不访问 provider、不补跑任何历史 Run。逐字输出：

```text
BASELINE=PARTIAL_RAW_USAGE_ACCEPTED model_calls=1 usage_calls=1
MODIFIED=USAGE_UNKNOWN model_calls=1 usage_calls=0
ROLLBACK=PARTIAL_RAW_USAGE_ACCEPTED model_calls=1 usage_calls=1
EXPLICIT_ZERO=KNOWN model_calls=1 usage_calls=1
SOURCE_ORIGINAL=UNCHANGED ROLLBACK_COPY=HASH_IDENTICAL PAID_CALLS=0
```

该命令 exit 0，日志为 `routine-usage-baseline-modified.txt`，同时证明真实零值仍可用、源码回退恢复
原行为、封存原件未被修改。源码与证据清单在 `routine-budget-freeze.json` 中冻结并重新打开核验。
底层测试不等同于真实 provider、部署或整个 PA-04 用户验收；Routine→Item 产品集成另由主模块验收。
