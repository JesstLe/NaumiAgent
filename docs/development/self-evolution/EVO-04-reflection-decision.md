# EVO-04 反思决策与防奖励投机

## 目标

基于结构化 before/after 证据决定 accept experiment、revise、reject 或 escalate，不让生成补丁的
同一个模型用叙事覆盖失败结果。

## 子模块

- EVO-04.1 Decision inputs：candidate、mutation receipt、Eval receipt、risk、user constraints。
- EVO-04.2 Mechanical gate：checks、guardrails、scope、budget、integrity 先判。
- EVO-04.3 Independent reviewer：可选不同模型/规则，看到证据但不能改结果。
- EVO-04.4 Counterfactual：是否有更小改动、改善是否来自删测试/改指标/放宽规则。
- EVO-04.5 Reward hacking detector：测试删除、阈值放宽、skip、mock 替代、数据泄漏。
- EVO-04.6 Decision state：accepted_experiment/revise/rejected/escalated。
- EVO-04.7 Reflection memory：只保存结构化经验和证据引用，禁止污染系统 Prompt。

## 验收标准

- mechanical gate 失败时 LLM 无权 accept。
- 修改测试/metric/config 使分数变好但产品不改善的 fixtures 被拒绝。
- reviewer 与 author identity、模型和 Prompt version 被记录。
- 同一证据/规则版本决策确定；LLM 补充意见不改变结构化终态。
- `escalated` 生成用户可理解选项和自定义输入，不擅自选择产品 tradeoff。
