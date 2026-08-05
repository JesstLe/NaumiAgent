# EVO-05.6a Automatic Pause and Rollback Request

## 目标

把 EVO-05.5b 的 exact `breached` Observation 转换为两个真实、可审计且幂等的动作：立即触发 workspace
rollout kill switch，并冻结只读 Rollback Request。该 Request 是 EVO-05.6b 回滚执行器的唯一输入，不等同于已回滚。

## 输入与机械门禁

协调器必须重新读取并验证以下持久化事实，不能相信调用方传入的叙述：

1. Observation 存在、摘要匹配、状态为 `breached`，且同时开放 pause/rollback input authority；
2. Observation 绑定的 stage entry、全部 terminal canary event 和 control event 仍存在且摘要一致；
3. Rollout Baseline 与 immutable Plan 仍 current，并与 Observation 的 ID/digest 完全一致；
4. Plan 绑定的 Fresh Promotion Input 仍存在，且携带的 prior Rollback Plan digest 与 Plan 冻结值一致；
5. kill switch 的最终状态必须为 `paused`。

任一门失败都不创建 Request。Observation 的自然语言原因不能替代上述证据链。

## 暂停语义

- 未暂停时以 `monitor/runtime_guardrail_breach` 创建 HMAC-attested control event；
- 已被用户、安全或数据事件暂停时复用原 pause event，保留原 actor/reason，不伪造 monitor 来源；
- `monitor_pause_created` 是持久化 pause 的 monitor/breach provenance 投影，不记录某个并发调用是否恰好抢先创建；
- 同一 Observation 在并发和重试下只产生一个 content-addressed Request；
- pause 会立即使原 local-canary entry 动态失去执行资格。

## Rollback Request

Request 绑定 Observation、Plan、Baseline、entry、Fresh Promotion Input、exact Rollback Plan、pause event 和全部
breach reasons。artifact 最大 512 KiB，使用 canonical workspace、排序去重 reasons 和内容摘要自验证。

它只授予 `rollback_request_authority=true`，并固定：

- `rollback_execution_authority=false`；
- `workspace_write_executed=false`；
- `git_write_executed=false`；
- `rollback_executed=false`；
- `promotion_authority=false`。

## 验收结果

- 真实 canary check failure 形成 breach 后，协调器自动暂停并冻结 exact Rollback Request；
- 八路并发重试返回同一 Request，不重复追加 pause；
- insufficient Observation 不能暂停或创建 Request；
- 已有 user pause 被原样复用；
- Engine 与公共 lazy exports 已接线；
- 只运行相关小模块测试，未运行全量测试。

## 下游状态与真实闭环边界

[EVO-05.6b1](EVO-05-6b1-immutable-rollback-source.md) 已消费本 Request，从 exact Git baseline 冻结
content-addressed rollback bytes。ARC-07.5a 仍需建立真实 version slot，EVO-05.6b2 再执行原子切换与启动验证。
EVO-05.7 随后写入 `promoted|rolled_back|superseded` Outcome，并把长期效果反馈回 Candidate/Eval。
完成二者及一次真实端到端演练前，不得宣称自进化真实闭环完成。
