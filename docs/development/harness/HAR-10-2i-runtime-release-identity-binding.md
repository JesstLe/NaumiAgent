# HAR-10.2i Runtime Heartbeat Release Identity Binding

## 目标

把 ARC-07.5e 已验证的 managed terminal runtime identity 与 New UI/TUI 的 exact heartbeat
`workspace/subject/instance/epoch` 绑定，使后续 EVO-05 opt-in observation 能证明样本来自哪个
pointer、slot、Boot Receipt 和 binary。本切片只建立发布身份权威，不把最新心跳冒充持续健康历史。

## 原子启动边界

Composition-owned `TerminalRuntimeLifecycleFactory` 是两端唯一装配入口。源码/开发运行没有 managed
launcher 环境时照常写 heartbeat，但不产生 binding；managed 环境则在启动时完成：

1. ARC-07.5e 重放 active chain、environment 与当前 executable；
2. 生成 content-addressed `HarnessRuntimeReleaseBinding`；
3. 在 Harness Store `BEGIN IMMEDIATE` 事务内同时写入 binding 与 sequence 1 `starting` heartbeat；
4. 之后才推进 sequence 2 `running` 和周期 pulse。

Binding 固定声明 `release_identity_authority=true`，同时固定
`heartbeat_liveness_authority=false`、`rollout_observation_authority=false`。通用 heartbeat schema 保持适用于
runtime、Agent、Browser 与 Pursuit，不被 ARC-07 字段污染。

## 失败与并发语义

- partial/mismatched managed environment 不签发 release authority；稳定错误码进入 lifecycle snapshot，
  但诊断 heartbeat 和 UI 启动继续工作；
- 同一 subject 的 exact startup 重试幂等；不同 binding、instance、epoch 或 startup payload 冲突失败；
- SQLite 写事务阻止“只写 binding”或“只写 starting heartbeat”的可见半状态；
- retention 删除 terminal/offline runtime heartbeat 时，在同一事务删除对应 binding，避免孤儿身份；
- binding 不授予工具执行、session 接管、回滚、stage completion 或 rollout percentage 权限。

## 双端与验收结果

- New UI 与 Textual TUI 使用同一 factory 和 Store 路径，各自生成独立 binding，但可指向同一 exact release identity；
- managed 双端真实 slot/launcher 夹具、identity 失败降级、startup 幂等/冲突/retention 清理 3 个测试通过；
- 既有 heartbeat、runtime services 与 terminal health parity 22 个定向测试通过；
- ruff、公共导入与 `git diff --check` 通过；未运行全量测试。

## 当前边界与下一步

HAR-10.2j 已建立 append-only、bounded page 的 release-bound heartbeat observation ledger；见
[HAR-10.2j](HAR-10-2j-runtime-release-observation-ledger.md)。它仍不证明最小持续时间、最小样本数、连续 exposure 或
中途是否出现 unhealthy gap。下一步由 EVO-05 独立聚合 observation window；不得从 binding 或单个 sample 直接签发
Stage Completion Evidence。
[EVO-05.5f5e](../self-evolution/EVO-05-5f5e-percentage-runtime-exposure.md) 已把 exact percentage Deployment、Binding 与
sequence 1/2 startup pair 组合成单 installation Exposure Receipt；该 Receipt 仍固定 completed-run/rollout authority 为 false。
