# CC-03.2b UI-local 状态语义映射

## 目标

补完 CC-03.2a 留下的 14 个 UI-local 行为单元格，使 loading、empty、focus、keyboard、cancel、detail
和 execution error 不再只由文档描述，而由 New UI 的真实 JavaScript 状态机、协议事件和目标测试共同证明。

本切片不新增第二套服务端 view model。Task、Permission、Doctor 的权威内容仍来自 CC-03.2a 的三个
typed snapshot；本清单只管理请求发出至终态到达之间的前端瞬态以及键盘/焦点控制态。

## 交付物

- `frontend/terminal-ui/cc-ui-state-mapping.v1.json`
  - 精确覆盖 `cc-semantic-mapping.v1.json` 中 owner 为 `ui_local` 的 14 格；
  - 绑定 CC-03.2a semantic mapping、protocol contract 和真实 `state.js` 摘要；
  - 为每格声明状态路径、触发器、client/server event、相关性要求和目标测试。
- `frontend/terminal-ui/scripts/verify-cc-ui-state-mapping.js`
  - 校验严格 manifest shape、精确覆盖、摘要绑定、协议列表与 event registry；
  - 运行 14 个真实状态转移 probe，而不是搜索函数名或仅检查 import；
  - 验证目标 Node 测试仍存在，输出确定性 audit 与稳定 finding code；
  - 全程只读，不启动 Bridge、模型、浏览器或后台 worker。
- `frontend/terminal-ui/test/cc-ui-state-mapping.test.js`
  - 覆盖正常、缺格、binding stale、registry/test 漂移、probe 失败和 CLI 只读路径。
- Task 面板请求态补强
  - `taskPanel.loading/error/requestId` 成为显式状态；
  - 首次请求立即显示加载卡；刷新保留最后一份好 snapshot；
  - 只有匹配 `request_id` 的 error 才结束当前 loading；
  - typed snapshot 或 legacy `ui/message(title=tasks)` 都能形成成功终态；
  - loading 与刷新失败在同一任务卡中以不同颜色显示。

## 14 格状态合同

| Area | Cell | 触发 | 本地权威状态 | 协议边界 |
| --- | --- | --- | --- | --- |
| Doctor | cancel | `c` | `probeRequestId/probeCancelRequestId/probeNotice` | `doctor/probe/cancel`，按 request id 等待 result/error |
| Doctor | empty | 初始状态 | `snapshot=null, loading=false, error=""` | 无 |
| Doctor | focus | `/doctor`, Escape | `route/originAnchor/scrollOffset/followTail` | 请求 `doctor`；返回纯本地 |
| Doctor | keyboard | `r` | `loading/error` | `doctor` |
| Doctor | loading | `/doctor` → snapshot | `loading/snapshot/error` | `doctor` → `doctor/health` |
| Permission | focus | `/permissions`, Escape | `route/originAnchor/scrollOffset/followTail` | 请求 `permissions_panel`；返回纯本地 |
| Permission | keyboard | `r` | `limit/loading/error` | `permissions_panel` |
| Permission | loading | `/permissions` → snapshot | `loading/snapshot/error` | `permissions_panel` → `permissions/snapshot` |
| Task | cancel | selected item cancel | `items/selectedId/selectedIndex` | `task_cancel` |
| Task | detail | open selected item | `selectedId/detailId/loading/requestId` | `task_panel(detail_id)` → snapshot/error |
| Task | error | correlated execution error | `activeTaskSubmission/messages[].taskStatus` | `task_submit` → `task/created` → `error` |
| Task | focus | focus request | `taskPanel.focused/inspector.focused` | 无；两个键盘消费者必须互斥 |
| Task | keyboard | selection movement | `items/selectedId/selectedIndex` | 无；按稳定 view id 导航 |
| Task | loading | `/tasks` → snapshot/error | `loading/error/requestId/snapshot` | `task_panel` → `tasks/snapshot` 或匹配 error |

Permission cancel 仍保持 CC-03.2a 的 `not_applicable`：只读权限中心不接管待决权限交互的拒绝按钮。

## 失败关闭规则

- semantic mapping 中 UI-local 格不再精确等于 14 格时，报告 `ui_local_coverage_mismatch`；
- semantic mapping、protocol contract 或 `state.js` 摘要漂移时报告 stale，不静默沿用旧证明；
- client/server event 不在协议列表或 event registry 中时报告 invalid；
- manifest 的 probe id、状态路径、trigger、事件或 correlation 与可执行合同不一致时报告 invalid；
- 目标测试不存在或真实状态转移断言失败时报告 invalid；
- 不相关 error 不得结束 Task snapshot loading；匹配错误不得清除最后好快照；
- Task 执行错误不得把已接纳任务退回为可再次创建的本地 outbox 消息。

## 校验命令

```bash
cd frontend/terminal-ui
node scripts/verify-cc-ui-state-mapping.js
node --test test/cc-ui-state-mapping.test.js
node --test --test-name-pattern='task panel can be pinned|task panel request state|typed task snapshot distinguishes' \
  test/state.test.js test/components.test.js
```

成功 audit 必须满足：

- `status=valid` 且 `finding_codes=[]`；
- `ui_local_cell_count=mapping_count=verified_probe_count=14`；
- `verified_target_test_count=16`（Task loading 同时覆盖手动刷新、自动刷新和最终渲染）；
- 同一输入重复运行产生相同 `audit_id`；
- verifier 运行前后 mapping、semantic mapping、protocol contract 与 `state.js` 字节不变。

## 自我审视与未完成项

- 本切片证明状态转移与协议相关性，不证明最终组件视觉已经完整消费全部状态；该责任属于 CC-03.3。
- Doctor 和 Permission 的基础 snapshot 请求目前没有保存独立 request id；它们按路由单请求状态收口。若未来允许
  同面板并发刷新，必须先扩展协议相关性，不能沿用当前证明。
- Task loading 兼容 typed snapshot 与 legacy `ui/message`，但 CC-03.3 应推动组件以 typed snapshot 为主，逐步缩小
  文本解析 fallback。
- 无色彩、窄屏、Windows 终端和 Textual TUI 的体验一致性仍属于 CC-03.6，不能由本轮 Node 状态测试替代。
- source-like fixture 与真实 Bridge fixture 的共同 golden 仍属于 CC-03.5。

下一小切片：CC-03.3a，先建立 Task 组件只消费 Bridge view model 与本地 presentation state 的边界证明；
避免一次性改造三个组件，并优先消除 Task legacy 文本解析对主路径的影响。
