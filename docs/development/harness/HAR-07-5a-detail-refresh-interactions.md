# HAR-07.5a Harness 详情权威刷新交互

## 用户结果

用户在 New UI 的 Harness 运行详情页中可按 `e` 单独刷新 Explain，按 `r` 单独刷新 Replay。
两项刷新互不阻塞：只加载需要的分区、保留当前滚动位置，并继续展示另一个分区最后一次成功读取的
权威事实。页面首屏直接展示键位，不依赖隐藏帮助。

TUI fallback 继续通过重新执行 `/harness detail <run-id>` 刷新相同 Store 权威数据；本切片没有在
Textual 的 Markdown 输出上伪造局部刷新状态。两种表面仍使用同一
`harness/explain/request`、`harness/replay/request` 和后端查询实现。

## 状态与请求契约

1. `e/E` 仅在 Explain 当前没有请求进行时发送一次精确 run id 请求；`r/R` 对 Replay 同理。
2. 请求携带本地最后接受的 `known_revision`。Bridge 即使发现 revision 相同也会重新读取并补发
   权威响应，因此刷新不是前端缓存假动作。
3. 同一分区 loading 期间的重复按键被幂等吸收，不制造请求风暴；另一分区仍可独立刷新。
4. 响应继续按 `run_id + revision` 进入既有有界缓存。相同 revision 不覆盖事实，但会结束对应
   loading 状态，向用户确认本轮读取已完成。
5. 刷新不执行模型、Harness check、工具或原任务，不改变 Receipt，也不自动重放副作用。

## 验收证据

- Node 状态测试覆盖大小写键位、精确 run id、已知 revision、分区独立 loading、重复按键去重与
  滚动位置保持。
- 页面测试覆盖 80/120/200 列下键位提示可见且不溢出。
- A3 真实链路由 Node 键盘状态机生成刷新请求，经真实 SQLite Harness Store 与 Python Bridge
  查询后，返回 Node normalizer、reducer 和详情渲染器；同 revision 仍可获得权威响应。
- 后端查询期间替换真实 check runner 为禁止调用函数，证明刷新不会重新执行验证。

## 自我审视与剩余边界

- 本切片实现了 HAR-07.5 的 Explain/Replay 刷新部分，没有宣称整个 HAR-07.5 完成。
- `v` Evidence 单项焦点、跨平台复制回执与完成卡直接进入详情已由 HAR-07.5b、5c1、5c2
  分别独立实现并验收。
- HAR-07.4b 仍依赖 Bridge 进程级重启、重新协商以及全局 sequence/gap 恢复基础；当前 UI 在
  Bridge 退出时会结束进程，不能把同进程刷新描述为断线恢复。
