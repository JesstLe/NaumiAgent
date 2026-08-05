# EVO-05.5d Durable Stage Advance Authorization

## 目标

把 EVO-05.5c 的 current Stage Completion 转换为一个短期、可审计、可 fencing 的
`local_canary → opt_in` 入口授权。本切片只决定“能否进入下一阶段”，不安装 candidate、不切换 version slot、
不部署、不发布，也不赋予 rollback 或 promotion 权限。

这一步消除两类伪闭环：模型口头声称“用户已同意”，以及 UI 回答完成后 control plane 已暂停但旧答案仍被继续使用。

## 手动 authority 链

high/critical 或计划要求人工推进时，`EvolutionRevalidationRolloutStageAdvanceService`：

1. 重验 exact Stage Completion 仍由 current Observation/Plan/Entry/terminal prefix 支撑；
2. 通过 Harness durable interaction 创建两个结构化选项：`advance` 或 `decline`；
3. interaction 绑定 Completion ID、owner lease、sequence、request digest 与 terminal answer；
4. 只接受 `answered_by=user` 的 option answer，不允许模型文本或 custom input 冒充授权；
5. 回答后再次重验 Stage Completion 和 current rollout control；
6. SQLite `BEGIN IMMEDIATE` 内再次比较 Completion digest 与 exact control generation；
7. 形成 content-addressed、按 Completion 幂等的 Stage Advance Receipt。

`decline` 同样被持久化，重复调用不会再次询问，也永远不产生 next-stage authority。

## 自动 authority 链

只有 Stage Completion 明确投影 `automatic_advance_eligible=true` 时才允许 automatic decision。自动路径与手动路径
写入同一种 Receipt、经过同一 Completion/control 原子检查，不创建或伪造用户 interaction。automatic 只能产生
`advance`，不能把策略缺失解释为默认通过。

## 动态 fencing

Receipt 绑定：

- exact Completion/Observation/Plan/Entry ID 与 digest；
- `local_canary → opt_in` 固定 transition；
- current control sequence/event digest；
- decision source、可选 durable interaction 和 request digest；
- `issued_at` 与短期 `expires_at`。

读取时重新投影 authority。过期、pause/resume generation 变化或 Completion 失效后，持久 Receipt 仍保留用于审计，
但 `next_stage_entry_authority=false`。bypass 不绕过这些机械门。

## 权限边界

- `advance` 只开放下一阶段入口 authority；
- `decline`、过期或 stale control 不开放任何入口 authority；
- `deployment_authority=false`；
- `rollback_authority=false`；
- `promotion_authority=false`；
- `git_write_executed=false`、`publish_executed=false`。

## 验收结果

- 真实 high-risk canary → passing Observation → Completion → Harness answer 链路可运行；
- 8 个并发调用只创建一次 durable interaction 并返回同一 Receipt；
- 用户拒绝形成持久无权限决定；
- 用户回答后 operator pause 会使 Receipt 创建 fail closed；
- authority 到期后动态 view 撤销 next-stage 权限；
- production Engine 与公共 lazy exports 已接线；
- 只运行 Stage Advance 和 Engine 小模块测试，不运行全量测试。

## 下一切片

opt-in Deployment Receipt 必须消费 current Stage Advance View，并绑定 approved candidate 的 exact commit/tree、
build manifest、installed version slot、rollback source 和 opt-in exposure cohort。只有完成真实安装、boot/self-test、
atomic slot activation 后才能声称 deployed；任何一步失败都不能制造 Deployment Receipt。
