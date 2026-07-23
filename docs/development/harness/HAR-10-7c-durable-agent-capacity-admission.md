# HAR-10.7c 跨 Runtime 的持久 Agent Capacity Admission

## 用户问题

HAR-10.7a/7b 只能限制单个 Python Runtime。用户同时打开两个 Naumi 进程时，两边都可能各自启动到本地
上限，等待队列也分别存在内存中；重启后排队事实消失。

本切片让生产 `SubAgentManager` 消费
[ARC-06.2c durable embedded capacity](../architecture/ARC-06-2c-durable-embedded-agent-capacity.md)：

- 多个 Runtime 共用 `AgentJobStore` 中的 active/waiting 上限；
- 模型调用前必须取得 durable claim；
- 无空位时进入有界 FIFO，而不是在另一个进程中越过上限；
- 用户可停止 `waiting_capacity`，取消事实持久保存；
- New UI、Textual 与 `/runtime subagent` 显示相同共享计数。

## 验收标准

- 两个 Runtime、共享上限 1：第二个 execution 可见为 `waiting_capacity`，模型调用计数保持 0；
- 第一项 terminal 后第二项自动取得 claim 并启动；
- 共享等待上限满时返回中文 overload 回执，不创建 partial Job；
- waiting stop 只取消目标 Job，不影响占用 active 的兄弟任务；
- expired running 不自动释放容量，并向 Agent Control 发出恢复警告；
- bypass 不绕过本地或 durable capacity；
- 所有显示只消费共享 authority，不由前端推断。

## 保留边界

这仍是 embedded Runtime：没有独立 Agent daemon、Worker Registry incarnation、物理资源隔离、跨主机
leader 或完整公平调度。HAR-10.7 保持 partial。
