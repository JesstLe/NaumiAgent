# EVO-01.3a Candidate Store Core

## 目标与边界

本切片把确定性的 `EvolutionCandidateDraft` 保存为可恢复、可审计的用户级状态，使同一机械
根因在多次运行、进程重启和并发投递后仍保持一个 Candidate。它不决定实验资格、不生成
修改、不执行 Review action，也不把 Candidate 写进 Agent 可修改的工作区。

数据库位于平台原生用户状态目录的 `evolution.db`：

- macOS：`~/Library/Application Support/NaumiAgent/evolution.db`；
- Linux：`${XDG_STATE_HOME}/naumi-agent/evolution.db`，无该变量时使用
  `~/.local/state/naumi-agent/evolution.db`；
- Windows：`%LOCALAPPDATA%\NaumiAgent\evolution.db`；
- 测试、便携部署和高级用户可用 `NAUMI_STATE_HOME` 显式覆盖。

路径解析只计算位置，不创建目录。第一次写入才建立状态目录和数据库；工作区内的 Agent
无法通过普通源码修改路径篡改候选审计队列。POSIX 首次写入将目录收紧到 `0700`、数据库
收紧到 `0600`。Windows 的等价 ACL 仍由 ARC-05.3/ARC-07 统一治理。

## Schema v1

`evolution_candidates` 保存每个规范工作区、Candidate identity、查询投影、完整 Draft JSON、
Draft SHA-256、修订号和时间窗。`evolution_candidate_evidence` 按工作区和 Evidence ID 唯一，
保存不可变 Evidence JSON 与 SHA-256。`evolution_candidate_events` 记录 `created` 或
`evidence_merged`、前后 Draft 摘要、本修订新增 Evidence ID 和发生时间。

三张表以 `(workspace_root, candidate_id)` 建立外键边界；同一工作区的 fingerprint 唯一，
不同工作区可以独立保存同一 Candidate identity。数据库通过 `PRAGMA user_version=1` 声明
版本，并以 `evolution.candidates` 注册到 ARC-05 Store Catalog。高于当前支持版本时失败关闭，
不会猜测降级。

## 合并与完整性规则

1. 写入前必须用 Evidence 重新构建 Draft；调用方伪造 occurrence、risk 或 metric 会被拒绝。
2. `BEGIN IMMEDIATE` 串行化跨 Store 实例写入；更新带旧 revision 条件并检查受影响行数。
3. 已存在且内容相同的 Evidence 是幂等重试，不增加 revision 或 audit event。
4. 已存在 Evidence ID 对应不同内容时失败关闭；不可变 Evidence 不允许覆盖。
5. 新 Evidence 只与同 candidate fingerprint 的旧 Evidence 合并，再由共享 builder 重建 Draft。
6. 每次读取校验 Draft JSON 摘要、投影列、Evidence JSON/摘要/数量，以及从 revision 1 到
   当前修订的完整 audit digest chain；任一不一致报告存储损坏。
7. 读取缺失 Store 是纯只读操作，不建立空数据库。

## 验收证据

- 100 次同根 observation 最终只有一个 Candidate、100 条 Evidence、revision 100；
- 10 个独立 Store 实例并发投递不同 observation，不丢失、不重复；
- 相同请求重试和旧请求晚到均不增加 revision；
- 不同工作区隔离，不同 root/scope 不误合并；
- 非规范 Draft、Evidence ID 内容冲突、未来 schema、无时区 clock 均失败关闭；
- 篡改 Candidate projection、Evidence digest 或 audit digest chain 均在读取时被检测；
- macOS/Linux/Windows 路径规则、惰性读取和 POSIX 权限有聚焦测试。

## 未完成项

EVO-01.3b 仍需定义 provider/model/platform 观察维度、时间窗策略和真实 collision fixtures；
EVO-01.4 才能产生独立的 eligibility decision。HAR-09 仍需 feedback adapter、阈值/冷却策略、
Review Queue action 和 HAR-08 outcome tracking。当前 Store 中的 Draft 始终
`experiment_eligible=false`，不能直接触发 `self_modify`。
