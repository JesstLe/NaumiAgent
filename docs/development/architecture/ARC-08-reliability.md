# ARC-08 可观测性、SLO 与灾难恢复

## 目标

为 Runtime、Bridge、worker、Store、provider、UI 和自进化建立统一可观测模型、服务等级目标、
故障演练和恢复手册。

## 子模块

- ARC-08.1 Correlation：session/run/task/agent/call/job/evidence 全链路 id。
- ARC-08.2 Structured events：状态、耗时、大小、结果分类，不含 secret/raw reasoning。
- ARC-08.3 Metrics：availability、latency、error、queue、resource、recovery、cost。
- ARC-08.4 SLO/error budget：交互、工具、恢复、Store、worker 各自目标。
- ARC-08.5 Alert/local notices：默认本地，用户可导出；不偷偷上传内容。
- ARC-08.6 Disaster scenarios：Store corruption、disk full、worker crash、provider outage、bridge split。
- ARC-08.7 Backup/restore：用户状态 manifest、加密可选、验证、部分恢复。
- ARC-08.8 Runbooks：检测、止损、恢复、验证、复盘和长期修复。

## 已完成前置

- UI-13.2a 已为 Provider 凭据、配置、401/403/404/429/5xx、timeout 与连接失败建立低基数稳定诊断码，
  且不携带 URL、模型、用户或请求身份。ARC-08 后续 metrics/alert/runbook 应复用这些分类，但在真实计数、
  时间窗口与采样合同落地前不得据此宣称 Provider SLO 已实现。
- ARC-04.5b1 已建立 OS credential-backed Runtime payload key 与 bounded AES-256-GCM envelope，作为
  durable Agent payload 和未来加密 backup 的共同安全原语；key rotation、迁移与灾难恢复仍未实现。
- ARC-04.5b2 已让 Agent request payload 进入加密 schema v1 Store，并区分 pre-start takeover 与
  running unknown recovery；Provider 对账、response recovery、retention 和告警仍未实现。

## 初始 SLO

- Runtime 本地启动成功率 ≥99.5%；正常机器 ready P95 <2s。
- 控制事件 P95 <100ms；取消可见 P95 <1s。
- Store 持久化成功率 ≥99.9%，失败时主任务结果保留率 100%。
- crash 后可恢复 run 的恢复成功率 ≥99%，未知副作用不得自动重放。

## 验收标准

- 每个灾难场景有自动故障注入和人工 runbook 演练记录。
- 指标标签有界，不以 run id 作为高基数全局 metric label。
- trace/export 脱敏扫描通过；关闭 telemetry 后无网络发送。
- 备份恢复到新目录后完整性检查、Explain/Replay 和 Session 查询可用。
- A5：24h soak 与季度灾难演练门禁。
