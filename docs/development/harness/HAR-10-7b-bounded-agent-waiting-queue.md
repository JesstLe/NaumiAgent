# HAR-10.7b 有界的进程内 Agent 等待队列

## 问题

HAR-10.7a 已让 direct、batch 和 DAG 入口共用同一个进程内并发门，但等待者数量仍可随输入无限增长。
当上游一次提交很宽的批次或多个调用者同时委派时，模型尚未开始工作，Runtime 就可能先积压大量协程与
上下文；此时 `/runtime subagent` 虽能显示排队数，却不能主动施加背压。

本切片补齐 embedded Runtime 的本地硬上限，为后续 ARC-06 持久 Worker scheduler 提供安全前置，但不把
内存队列冒充跨进程集群调度。

## 实现契约

- `safety.max_queued_agents` 默认 64，允许 0 至 10000；0 表示活跃槽位耗尽后立即拒绝，不允许等待。
- `SubAgentManager` 同时约束 `max_parallel_agents` 个活跃执行与 `max_queued_agents` 个等待者。direct、
  batch 和 DAG 都调用公开 `delegate()`，不能绕过同一预算；`bypass` 不绕过资源容量。
- admission 检查与排队计数更新之间没有 `await`，同一事件循环中的并发调用不能超卖最后一个等待位。
- 容量耗尽时返回稳定 `AgentResult(status="error")`，中文回执明确给出等待上限；直接委派同时发送
  `subagent_event(status="failed")`，便于现有 UI 呈现失败原因。
- 批量入口只为当前共享预算可接受的前缀创建任务，溢出项按原输入位置返回错误；接受项仍维持 FIFO
  admission 与稳定结果顺序，单项失败不取消兄弟任务。
- 等待取消必须归还 queue 计数；活跃完成、异常、超时或取消必须归还 semaphore 与 active 计数，后续
  委派可立即复用容量。
- `/runtime subagent` 展示 `活跃/上限 · 排队/上限`，并明确标注这是“进程内 Agent 并发”。

## 验收标准

- 活跃 1、等待上限 1 时，第三个 direct 委派立即得到明确过载回执；前两个释放后所有计数归零。
- 等待上限 0 时，第二个 direct 委派不进入等待态。
- 活跃 2、等待上限 2 的六任务批次只接受前四项，后两项稳定返回错误；执行峰值不超过 2。
- 两个同时批次共享活跃与等待预算，合计只接受两个任务，不因调用者数量超卖。
- 满队列中的等待者被取消后，替代任务可复用空出的等待位；既有 parent/active cancellation 测试继续通过。
- 配置默认、边界值、引导生成、示例配置和 Runtime 用户文案保持一致。

## 明确保留边界

- 队列仅存在于当前 Python Runtime 内；重启会丢失等待者，也不能协调多个进程或主机。
- 当前只有 FIFO semaphore，没有 priority、deadline、aging、workspace/provider 配额、持久 overload receipt
  或跨客户端公平性；这些仍属于 ARC-06.2/06.4。
- Agent 尚未作为 ARC-04 持久 Worker producer 消费 Registry reservation；因此本切片不能宣称完成
  Agent cluster dispatch、crash recovery 或全局容量治理。
- Browser TaskRunner 和工具批次继续使用各自的 admission，本切片不合并不同资源池。
