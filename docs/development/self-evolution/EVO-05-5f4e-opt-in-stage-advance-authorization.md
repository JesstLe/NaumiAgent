# EVO-05.5f4e Opt-in Stage Advance Authorization

## 目标

把 current passing EVO-05.5f4d Stage Completion 转换为短期、可审计、可 fencing 的
`opt_in → percentage` Stage Entry Authorization。本切片只回答“是否允许进入 percentage 阶段”，不执行部署、流量扩大、
发布、Git 写入、stable rollout 或 promotion。

## Exact 输入与决策策略

授权必须同时重验：

1. exact Stage Completion Evidence ID/digest/source-set 仍是 current passing authority；
2. current Rollout Plan ID/digest 与 Evidence 一致，完成阶段严格为 `opt_in`，下一阶段严格为 `percentage`；
3. Plan 冻结的 percentage exposure 与 opt-in `manual_advance_required`；
4. rollout control 的 exact sequence/event digest 仍为 `active`；
5. 手动路径的 Harness interaction 仍是 authoritative terminal user option answer。

high/critical、数据备份策略要求人工推进时，只接受 `advance` 或 `decline` 两个结构化选项，禁止 custom input 和模型文本冒充。
low/medium 且 Plan 明确 `manual_advance_required=false` 时走 automatic path；automatic 不创建 interaction，也只能产生 `advance`。

## Durable interaction 与并发

- interaction subject 绑定 exact Stage Completion Evidence，而不是宽泛 candidate 或会话；
- interaction ID、request digest、owner lease、sequence 和 `answered_by=user` 一并冻结；
- 同一 Evidence 的并发调用复用 deterministic interaction identity；遇到另一服务实例已创建的 pending interaction 时，执行有界
  authoritative-state wait，回答提交后复用 terminal record；
- SQLite `BEGIN IMMEDIATE` 内再次核对 Completion、Plan 与 rollout control durable row；
- 同一 Evidence 只允许一个持久决定。相同决定的并发 Receipt 收敛到首个 commit，不同决定 fail closed。

## Receipt 与动态撤权

Receipt 使用 canonical JSON 与 SHA-256 content identity，冻结：

- Stage Completion Evidence、原始 completion、Plan 和 source-set identity；
- `opt_in → percentage` transition 与 percentage exposure；
- manual/automatic policy projection；
- control generation、interaction/request digest、decision source；
- `issued_at`、`expires_at` 与所有负权限边界。

View 每次重新读取 Receipt、Stage Completion、Plan、control 和 interaction。Evidence 更新/失效、Plan 漂移、pause/resume generation
变化、interaction 篡改或 authority 到期后，历史 Receipt 继续保留，但 next-stage/percentage-stage-entry authority 即时撤销。

## 权限边界

`advance` 且全部 current checks 成立时，只开放：

- `next_stage_entry_authority=true`；
- `percentage_stage_entry_authority=true`。

以下能力始终为 false：

- `percentage_rollout_authority`（尚未执行人口分配或流量扩大）；
- `deployment_authority`；
- `stable_rollout_authority`；
- `rollback_authority`、`promotion_authority`；
- `git_write_executed`、`publish_executed`。

## 验收结果

- 复用真实 Candidate → Deployment → Liveness → managed ChatRun → Outcome → 5f4d Aggregation fixture；
- current fixture 的 GREEN Baseline 为 `no_model_execution`，先验证 5f4d 正确 fail closed，再用严格、content-addressed 的 passing
  projection 隔离验证本授权层；该 projection 不冒充生产 live-cost baseline 证据；
- 两个 Service 实例共六路并发调用收敛到同一 durable user answer 和 Receipt；
- `advance` 只开放 percentage stage entry，`decline` 永不开放 authority；
- automatic Receipt 不伪造 interaction，也不开放 percentage rollout/deployment authority；
- expiry 与 operator pause 均动态撤权；伪造 percentage exposure 被 current Plan source check 拒绝；
- ruff、compile、公共 lazy import 与本小模块测试通过；未运行全量测试。

## 当前边界与下一步

EVO-05.5f4e 只签发入口 authority，尚未创建 percentage population assignment，也没有执行 release exposure。
[ARC-07.5c](../architecture/ARC-07-5c-signed-installation-population.md) 已补齐不能由客户端编造的 signed population snapshot 前置。
[EVO-05.5f5a](EVO-05-5f5a-percentage-cohort-assignment.md) 已同时消费 current 5f4e View、current Snapshot 与安装
proof-of-possession，以稳定、可复现且隐私安全的 ranked hashing 形成 exact limited cohort；Assignment 自身仍不声称已部署或已观察。
