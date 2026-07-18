# UI-18.4b Textual TUI Durable Interaction Parity

## 目标

让 fallback Textual TUI 与 New UI 使用同一个 HAR-10.6 interaction authority，而不是只把问题保存在
`ModalScreen` 的内存 Future 中。正常回答、实时 timeout、进程重启、旧 owner takeover 和 Pursuit checkpoint
回填必须保持同一顺序与 fencing 语义。

本切片不新增另一套问题 schema，也不让 TUI 自动恢复模型运行。

## 共享 authority adapter

`DurableInteractionAuthorityClient` 封装两类 UI host 共用的机械协议：

- create：构造严格 record 并在显示前写入 authority；
- answer：按原问题重新校验答案，再执行 sequence/owner/epoch fenced commit；
- expire：显式提交 pending→expired；
- recover：有界读取最多 50 项，过期问题先 expire，活 owner 延后复查，死 owner takeover；
- renew：Bridge/TUI 在用户思考或问题排队期间按 lease 的三分之一周期同 owner 续租；回答/expire 前停止
  keepalive 并使用最新 sequence，避免 30 秒后误拒合法答案；
- remaining timeout：从 durable `expires_at` 计算 UI 等待时限。

Bridge 已改用此 adapter，TUI 不复制 Store 状态机。两端仍分别拥有各自的卡片/Modal 生命周期。

## TUI 实时链路

1. `AgentEngine` 提供 stable ID、subject 与 task-local Pursuit callback；
2. TUI 先 create authority，随后调用 Pursuit begin checkpoint，再显示 Modal；
3. 多个问题通过 `_interaction_lock` 串行显示，但每个问题在等待 UI 锁之前已经落盘并启动 owner keepalive；
4. 选项/自定义答案先提交 authority，再调用 Pursuit resolve checkpoint，最后返回工具；
5. timeout 使用 durable deadline，dismiss Modal 并显式 expire；
6. authority answer 失败时不伪装成功；answer 已保存但 checkpoint 失败时明确提示 `/pursue resume`。

## 启动恢复

TUI mount 后启动非阻塞 recovery worker：

- workspace 隔离读取 pending questions；
- 不抢占仍持有有效 lease 的 Bridge/TUI owner；
- lease 到期后以 `tui-*` owner 和新 epoch takeover；
- recovered questions 复用同一个键盘上下选择、自定义输入 Modal；
- 答案保存后只提示用户显式 `/pursue resume`，不会隐藏创建第二个 Pursuit executor；
- 同进程 live interaction ID 集合防止 startup worker 与实时调用重复展示。

## 验收证据

- 原有上下键 option 与 custom Modal 用例继续通过，并返回 authority 规范答案；
- TUI callback 在 create 后观察到 pending，在 resolve 前观察到 answered；
- Bridge/TUI 等待期间均实际推进 owner sequence，停止 keepalive 后仍可用最新 sequence 回答；
- 旧 Bridge owner 过期后由 TUI takeover 为 epoch 2，同 stable ID 成功回答；
- deadline 到达后 authority 进入 expired，调用方收到中文 timeout；
- answer 已提交但 Pursuit checkpoint callback 失败时，authority 保留 answered，TUI 明确提示
  `/pursue resume`；
- HAR-10.6b 的 Bridge/Pursuit interaction 定向用例无回归；
- Python TUI/Bridge/Harness/Pursuit/Composition interaction 定向子集 44 项通过；
- 仅运行 TUI interaction、Bridge/Harness/Pursuit interaction、ruff 与定向 import；不运行全量测试。

## 当前不足

- Goal 页面尚未汇总 pending/expired interaction 历史，也没有显式 takeover/cancel 按钮；
- TUI recovered answer 不自动执行 `/pursue resume`，这是避免隐藏 owner 竞争的刻意边界；
- pending recovery 仍是 50 项有界批次，没有 cursor、优先级或 immediate-message 插队；
- TUI Modal 只显示问题 deadline 结果，尚未显示倒计时；跨平台窄终端布局由 UI-16 继续验证。

UI-18.4 仍保持 partial。下一步应在 `UI-18.4c Goal interaction state/actions` 与
`HAR-10.3a durable immediate-message queue` 之间按用户价值和依赖选择一个最小切片。
