# EVO-05.5f5x2 Stable Rollout Authorization

## 目标

将 current 5f5w Population Completion、单 installation member 的 current 5f5x1 binary rollback readiness 与 current
rollout kill-switch generation，冻结为短期、single-use、binary-only Stable Rollout Authorization。

Population Completion 已证明 exact signed Population 的全部成员完成 stable-stage；Authorization 仍按 member 签发，避免把最多
一万成员的执行能力压进一个超大 bearer artifact，并让未来执行器可以逐 member 对账、消费与恢复。

本切片只授权 `finalize_stable_population_member`。它不执行 finalization，不修改 pointer，不允许配置/数据变更，也不授予
deployment、rollback 或 promotion authority。

## Durable authorization

Authorization 冻结 Completion/source-set/Snapshot、member/Intent、Readiness、expected active pointer、rollback slot/Boot Receipt、
kill-switch event chain、attempt/previous authorization、32-byte start nonce、60–900 秒 TTL 与允许的唯一 operation。ID/SHA 由
canonical JSON 计算；source-set SHA 只绑定动态 sources，不包含 nonce 或 wall clock。

同一 Completion/member/source 在未过期且未消费时并发重试返回同一票据。过期或消费后可以签发下一 attempt，新票通过
previous ID/SHA 形成可审计链，不覆盖历史事实。

## Writer fence 与单次消费

Store 使用同一 Evolution SQLite 的 `BEGIN IMMEDIATE`：

- 写入前重读 exact Completion durable SHA；
- 在同一事务重读 latest rollout-control generation；
- `(completion, intent, attempt)` 唯一，跨 Service 并发只能形成一张 current ticket；
- consumption 表对 `authorization_id` 唯一，原子保证只消费一次；
- 同一 consumer + nonce 的重试幂等返回原 Consumption Receipt，其他 consumer 或 nonce 稳定拒绝。

Service 在消费前动态 inspect；Store 再检查 expiry、nonce 和唯一 consumption。未来 executor 仍必须在执行 CAS/最终化前重验
Authorization 和 expected pointer，不能把一次 inspect 当作事务锁。

## 动态撤权

`inspect()` 每次重验 durable source、5f5w current authority、5f5x1 readiness、kill-switch exact generation、TTL 和 consumption。
Completion/readiness/pointer 漂移、pause/resume 产生新 generation、过期或消费都会令 `stable_rollout_authority=false`；历史票不删除。

固定边界：

```text
operation_scope = binary_only
config_data_mutation_allowed = false
deployment_authority = false
rollback_authority = false
promotion_authority = false
```

## Agent Tool 与 Slash

- Tool：`evolution_stable_rollout_authorization`；
- 签发：`/evolution stable-rollout-authorization issue <completion-id> <intent-id>`；
- 重验：`/evolution stable-rollout-authorization inspect <authorization-id>`；
- 两条通道共享 Service 与 renderer；签发只写审计 authority，不执行外部动作，无二次确认；lockdown 禁止；
- nonce 保存在 durable artifact 供 future executor 精确消费，但 renderer 不显示 nonce。

## 验收与边界

- 六路、两个 Service 的并发 issue 收敛为一张 durable Authorization；
- real Completion + Release Slot readiness 全链签发，strict JSON round-trip；
- exact consumer retry 幂等，异方消费拒绝，消费后动态撤权；
- kill-switch generation 变化立即撤权并阻止新签发；
- expiry 后旧票保持撤权，新票形成 attempt/previous chain；
- Engine、Tool、Slash、权限、public exports、Ruff、compile/YAML 与相关小测试通过；不运行全量测试。

当前尚无 `finalize_stable_population_member` executor，因此本切片没有谎称 stable rollout 已完成。下一最小切片必须由 executor
消费 exact nonce/Authorization，在动作前重验 expected pointer，并形成独立 Completion Receipt；配置/数据 finalization 继续等待
ARC-07.6，Promotion authority 继续独立。
