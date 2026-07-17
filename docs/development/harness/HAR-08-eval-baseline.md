# HAR-08 Eval Suite、Baseline 与回归比较

## 目标

建立可重复、可预算、可解释的评测系统，用于比较模型、Prompt、Tool、Harness、UI 协议和
自进化补丁。机械指标优先，LLM Judge 只能补充且必须记录模型/Prompt 版本。

## 子模块

- HAR-08.1 Eval schema：suite/case/input/fixture/expected/metrics/budget。
- HAR-08.2 Static runner：无模型的规则、Schema、知识索引与协议评测。
- HAR-08.3 Replay runner：复用 HAR-05，不执行副作用。
- HAR-08.4 Sandbox runner：临时目录/worktree 的真实 Tool 与 Check。
- HAR-08.5 Live runner：显式 `--live`、成本/时长上限、provider identity。
- HAR-08.6 Baseline identity：commit、config digest、model capability、platform。
- HAR-08.7 Comparator：绝对门槛、相对变化、置信区间、波动标记。
- HAR-08.8 CLI/Tool/API：`/harness eval|baseline` 与只读查询工具。

## 指标

完成率、verified rate、工具成功率、重复率、权限阻断率、token、成本、延迟、上下文峰值、
恢复成功率、UI 协议错误率。每个 case 指定主指标和 guardrail，禁止只优化单一总分。

## 验收标准

- 至少 5 个离线 fixture 和 1 个真实 NaumiAgent 小模块；重复两次结果可比较。
- 相同 baseline identity 不可被不同配置覆盖；变更必须创建新版本。
- Live 默认关闭；超预算立即停止并产出 partial receipt。
- 随机性 case 至少重复 5 次，报告均值、离散度和样本数。
- Eval 自身错误与被测实现失败分开分类。
- A4：macOS/Linux/Windows 的 Static/Replay 一致；Sandbox 差异有平台说明。

## 存储

在 H5 才新增 `harness_eval_results` 与 baseline 表，迁移幂等；大 artifact 只保存 URI/digest。

## 分阶段实现

- HAR-08.1a 离线协议 Eval：设计完成，实施见
  `HAR-08-1a-offline-protocol-eval-design.md` 与
  `HAR-08-1a-offline-protocol-eval-implementation-plan.md`。
- Replay/Sandbox/Live/Baseline/Comparator 与完整存储仍为 planned；1a 完成后也不得把 HAR-08
  整体标记为 implemented。
