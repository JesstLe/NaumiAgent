# HAR-10.3b3 历史 Queue Claim 人工处置

## 目标

让用户能审查 HAR-10.3b2 安全阻断的历史 claim，并显式选择重试或放弃。处置不能删除 lease 历史、复用
旧 request identity，或覆盖仍存活的 owner；New UI 必须在处置后立即刷新同一持久队列，CLI/TUI fallback
则复用同一 `/queue` authority 和中文回执。

## 权威状态机

1. `/queue list` 从 Harness Store 读取当前 Session 的有界队列，并展示 request、消息摘要、owner、epoch、
   lease 到期时间和“活跃/已过期/已释放”机械结论。
2. active 且 `expires_at > now` 的 claim 一律拒绝 retry/cancel；界面不能把 owner 暂时无响应推断为死亡。
3. `cancel` 仅接受过期或 released claim：原 queue item 终结为 `cancelled/explicit_cancel`，lease 保留并释放，
   resolution 记录审查到的 owner/epoch/state、actor、reason 和时间。
4. `retry` 在同一 SQLite 事务中终结旧 item、释放旧 lease、写 resolution，并以确定性新 request id 和原文本
   新建 queue item。新 item 保留 FIFO 位置但从未 claim，因此可以安全进入正常 claim/fencing 路径。
5. 同一处置 identity 幂等返回；审查后 lease owner/epoch 变化、request 已终结或 retry identity 被占用均
   fail closed。
6. New UI 完成 `/queue resolve` 后强制重读 Store，移除旧本地条目并立即派发安全前缀；默认新进程仍不会
   自动恢复未显式 resume 的 Session。

## 数据与界面

- Harness Store v15 新增 `harness_conversation_queue_resolutions` 审计表；原 v14 queue 和 RunLease 表不重写历史。
- `/queue list` 是只读审查；`/queue resolve <request-id> retry|cancel [原因]` 本身就是显式用户决策，不再增加
  二次确认。bypass 与其他模式都不能绕过 live-owner fencing。
- New UI、CLI 和 Textual TUI 都从共享 slash router 调用 Python authority；只有已经具备运行中队列的 New UI
  会在 retry 后立即派发。TUI 尚未支持“模型运行中继续输入”，因此不会伪装具备该交互能力。

## 验收证据

- live claim 在 review 中显示不可处置，retry/cancel 不改变 queue、lease 或审计表；
- expired claim retry 原子生成新 identity，旧 item/audit/lease 可追溯，新 item 可被 epoch 1 正常 claim；
- released claim cancel 幂等，后续安全队列前缀可恢复；
- New UI 从 blocked resume 执行 `/queue resolve ... retry` 后，使用新 request id 立即运行原消息并完成 fenced terminal；
- Store v14 及更老数据库无损升级到 v15，既有 queue payload 摘要继续兼容；
- 命令注册表、补全和 fallback 均包含 `/queue`。

## 当前不足与后续

- TUI 运行中输入和自动派发仍未实现，属于独立 UI 前置，不能仅靠 Store 接入声称 parity；
- 多客户端公平、优先级、cursor/分页、retention 与普通 queued item 的显式取消仍未完成；
- resolution 当前保存必要审计元数据，不保存模型输出或原始工具结果；进一步证据查看可并入 UI-10.5 Timeline。
