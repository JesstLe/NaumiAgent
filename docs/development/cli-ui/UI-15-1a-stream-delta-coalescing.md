# UI-15.1a 有界 Stream Delta 合并

## 目标

降低高频模型输出对新 UI 主线程的压力：连续 assistant token 与 thinking delta 不再逐条执行完整
`reduce → timeline anchor → UI snapshot → persistence schedule → redraw schedule`，而是在一个短帧窗口中精确拼接。
权限、人工交互、工具结果、错误与运行终态仍必须保持协议顺序并立即可见。

## 设计

### 合并资格

仅合并相邻且 identity 一致的两类事件：

- `ui/message(type=assistant_stream, phase=token)`；
- `ui/message(type=thinking, phase=delta)`。

identity 由消息类型、phase 和 `request_id` 组成。不同请求、thinking/assistant 切换、start/end 或任何其他
事件都会先同步 flush，禁止跨语义边界拼接。

### 有界策略

- 默认 flush 窗口 8ms，最长不会等待一个 16ms paint frame；
- 单批正文最多 65,536 UTF-16 code units，超过上限先交付旧批次；
- 单批最多 2,048 个 delta，空 token 也不能形成无界 pending count；单个超限 token 不拆分，直接交付 reducer；
- 合并后保留最后事件 seq/id/request_id，正文严格按到达顺序连接；
- timer 使用 `unref()`，不会阻止进程退出；正常退出、Bridge 断连和协议解析错误会先 flush；fatal restore 会丢弃
  尚未交付的本地 delta，避免在损坏状态上继续 reducer。

### 控制屏障与绘制

所有非 delta 记录都是同步顺序屏障。用户阻塞或终态事件（permission/interaction request、error、tool_result、
run terminal、completion receipt）还会取消已排队 paint 并立即执行一次 differential screen paint，避免短事务中
工具结果被后续完成页覆盖、用户从未看到关键控制状态。

## 验收证据

- 1,000 个连续 token 只调用 reducer 一次，正文长度、顺序和最后 seq 完整；
- start → 1,000 token → end 仍按三个语义记录交付；
- permission request 会先 flush token，再同步交付权限，timer 不残留；
- 不同 request、assistant/thinking 不合并；65,536 上限可机械触发分批；
- 主 PTY 真实场景完成模式切换、权限、tool prepare/use/result、折叠 diff、run terminal 和 completion receipt，
  证明 `+new` 工具结果在完成前实际绘制且最终回执无回归。

## 当前边界

- progress/todo/runtime status 仍逐条 reducer；在定义“只保留最新”还是“累计”的领域语义前不能盲目丢事件；
- 尚未交付 UI-15.6 可重复 CPU/RSS/P95 benchmark，因此本切片证明 reduction 次数，不宣称完整性能 SLO；
- Textual TUI 不消费 Node JSONL reducer，本切片不改变其刷新模型；UI-17 前仍需独立 TUI 高频输出基线。
