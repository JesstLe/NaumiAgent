# 决策、假设、风险与阻塞台账

模块文档定义目标，本文定义实施过程中如何记录会改变多个模块的判断。不要把架构决定藏在
commit message、聊天记录或代码注释里。

## 1. ADR 命名

跨模块决策使用 `ADR-NAUMI-NNN-短名称.md`，放入现有架构决策目录；至少包含：背景、约束、
候选方案、量化证据、决定、后果、回滚条件、受影响模块 ID、复审日期。

以下情况必须写 ADR：

- Ink/current renderer 的 adopt/defer/reject；
- embedded/runtime service 的默认切换；
- daemon 语言和进程边界；
- Store/Protocol major version；
- 闭源分发、更新信任根和签名策略；
- 自进化自动审批权限扩大；
- 改变唯一权威归属或跨模块依赖方向。

## 2. 风险记录格式

```yaml
risk_id: RISK-<MODULE_ID>-NN
title: <具体失效模式>
owner: <module owner>
probability: low|medium|high
impact: low|medium|high|critical
trigger: <可观测触发条件>
mitigation: <预防措施>
contingency: <触发后的动作>
evidence: <test/metric/runbook/artifact>
status: open|mitigated|accepted|realized|closed
review_at: <stage gate or date>
```

“可能有 bug”“注意性能”不算风险；必须描述具体资产、失效方式、用户影响和检测信号。

## 3. 初始跨模块风险

| ID | 风险 | 主要模块 | 预防与验收 |
| --- | --- | --- | --- |
| RISK-X-01 | UI 与 Runtime 各自推导状态导致恢复后分叉 | ARC-03, UI-17 | revision/full snapshot/golden resume |
| RISK-X-02 | Replay 或 retry 重复执行破坏性工具 | HAR-05, ARC-04 | side-effect classification、idempotency、调用计数 0 |
| RISK-X-03 | 高并发形成 provider 重试风暴 | ARC-06 | shared backoff、retry budget、故障注入 |
| RISK-X-04 | Harness/Eval 持久化 secret 或 reasoning | HAR-05/08, ARC-08 | schema allowlist、redaction、导出扫描 |
| RISK-X-05 | 自进化优化 Eval 而破坏真实用户体验 | EVO-03/04/06 | holdout、guardrail、反奖励投机、canary |
| RISK-X-06 | 闭源包仍携带源码、符号或凭据 | ARC-07 | unpack audit、SBOM、secret scan、签名验证 |
| RISK-X-07 | TUI fallback 随 New UI 演进而失能 | UI-17 | capability manifest 和双端 golden scenario |
| RISK-X-08 | Claude Code 上游迁入覆盖 Naumi 权威 | CC-01/03/05 | provenance、adapter、行为契约、人工 diff |
| RISK-X-09 | migration 中断造成旧会话不可恢复 | ARC-05 | copy-on-write、journal、rollback fixture |
| RISK-X-10 | 心跳短抖动误杀长任务 | HAR-10, ARC-04/06 | lease grace、多周期判定、pause/drain 测试 |

## 4. 假设台账

每个未经证实但影响设计的前提使用 `ASM-<MODULE_ID>-NN`，记录验证命令、最迟验证阶段和假设
失败时的替代方案。例如“Windows ConPTY 支持某 ANSI 能力”必须通过 capability probe，而不是
根据平台名硬编码。到阶段门仍未验证的高影响假设自动转为 blocker。

## 5. 阻塞与解除

阻塞项必须写明：阻塞模块/子模块、已完成工作、外部依赖、三次重复证据（若要正式标 blocked）、
解除条件、可继续的无风险工作。依赖未开始不等于可以越过门禁；实现模型应切换到依赖已满足的
其他模块，由计划维护者更新 owner。

## 6. 决策复审

在 Wave 4 renderer 决策、Wave 5 Runtime 服务化、Wave 6 自进化 promotion、Wave 7 发布候选前，
必须复审所有 open/accepted 风险和仍有效 ADR。若事实、benchmark 或安全边界已经变化，创建新
ADR supersede 旧决定，不能静默改写历史。
