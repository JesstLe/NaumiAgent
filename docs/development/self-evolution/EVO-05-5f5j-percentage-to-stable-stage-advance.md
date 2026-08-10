# EVO-05.5f5j Percentage-to-Stable Stage Advance Authorization

## 目标

把 current passing [EVO-05.5f5i](EVO-05-5f5i-percentage-completed-run-aggregation.md) Stage Completion 转换为短期、
可审计、可 fencing 的 `percentage → stable` Stage Entry Authorization。本切片只回答“是否允许进入 stable 阶段”，不执行
部署、100% 流量切换、发布、Git 写入、promotion 或 rollback。

Rollout Plan 明确要求 stable 永远人工推进，因此 v1 不存在 automatic stable advance。`bypass` 只影响工具权限，不得绕过
发布治理的 durable user decision。

## Exact 输入与人工决策

授权必须同时动态重验：

1. exact 5f5i Evidence ID/digest/source-set 仍是 current passing authority；
2. current Rollout Plan ID/digest 与 Evidence 一致，完成阶段严格为 `percentage`，目标严格为 `stable`；
3. stable stage 的 exposure 固定为 100%，且 `manual_advance_required=true`；
4. rollout control 的 exact sequence/event digest 仍为 `active`；
5. Harness interaction 是 authoritative terminal option answer，`answered_by=user`。

交互只接受 `advance` 或 `decline`，禁止 custom input、模型文本和工具输出冒充决定。问题明确展示真实 observed/successful runs、
error rate 和 100% target，并说明选择只签发 entry authority，不会执行部署或发布。

## Durable interaction、并发与重放

- interaction subject 精确绑定 5f5i Evidence，而不是 candidate、session 或宽泛 workspace；
- deterministic ID 冻结 Evidence suffix 与 attempt sequence，request digest 排除 timeout 后保持稳定；
- Harness authority 冻结 owner ID/epoch/lease、answer sequence、`answered_by=user` 与 terminal timestamp；
- 两个 Service 实例并发时复用同一 pending/answered interaction，并有界等待 authoritative state；
- 交互历史最多读取 100 条，attempt 最大 9999，异常、多答案或 pending 超时均失败关闭；
- 用户回答后再次读取 Completion、Plan 与 control projection，关闭决策期间的 TOCTOU 窗口。

## Receipt、Store 与动态撤权

Receipt 使用 canonical JSON 与 SHA-256 content identity，冻结：

- Evidence/Assignment/Plan/source-set identity；
- `percentage → stable` 与 100% target；
- mandatory manual policy projection；
- control generation、完整 interaction、request digest 与 decision；
- `issued_at`、最长 86400 秒的 `expires_at` 和全部负权限。

Store 限制 Receipt 为 512 KiB，在 `BEGIN IMMEDIATE` 内以参数化 SQL 重读 Completion、Plan 与 latest control，并以 Evidence
唯一约束保证一份持久决定。相同决定并发幂等，不同证据或 source 冲突失败关闭。View 每次重读 Receipt、Completion、Plan、
control 与 Harness interaction；Evidence 更新、Plan 漂移、pause/resume、interaction 篡改或 TTL 到期都会立即撤销 authority。

## 权限边界

只有 `decision=advance` 且所有动态 source current 时开放：

- `next_stage_entry_authority=true`；
- `stable_stage_entry_authority=true`。

`stable_rollout_authority`、`deployment_authority`、`promotion_authority`、`rollback_authority`、`git_write_executed` 和
`publish_executed` 始终为 false。`decline` 是不可变审计事实，但永不开放 authority。

## 验收结果

- 复用真实 5f5a→5f5i、30 个 managed ChatRun 与 percentage aggregation fixture；
- current GREEN fixture 因成本不可比先诚实 insufficient，再以隔离、content-addressed passing projection 验证授权层；
- 两个 Service 实例六路并发收敛到同一 durable user answer 和 Receipt；
- advance 只开放 stable entry，decline 永不开放 authority，interaction 明确禁止 custom input；
- 伪造 Plan digest 被 current source check 拒绝；TTL 与 operator pause 动态撤权；
- 非法 Evidence ID 在创建锁或访问 SQLite 前拒绝；ruff、compile、公共 lazy import、YAML 与单个真实小模块测试通过；
  未运行全量测试。

## 当前边界与下一步

5f5j 只签发短期 stable entry authority，尚未形成 stable release intent、执行 100% target activation 或观察 stable runtime。
下一最小切片应先进行依赖审计，再实现 `EVO-05.5f5k Stable Deployment Intent`：绑定 current 5f5j、exact admitted release、
active pointer/control generation 与 host target，仍不把 Intent 冒充已部署。
