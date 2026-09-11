# 10｜Harness、可观测性与评测：怎样证明一次改动有效

[返回目录](README.md) · [阅读方法与术语](17-阅读方法与术语.md)

> 读者目标：能区分运行 Harness 和评测 Harness，设计环境终态断言、指标和公平的前后比较。
> 题目为按工程问题设计的模拟题，不冒充任何公司的真实面试记录。

## 从一个具体问题开始

先设想一个代码修复 Agent：它回复“测试通过”，但你打开仓库发现测试从未运行。要判断它做得怎样，不能只看最终答案。你需要知道它调用过什么工具、文件如何变化、哪些检查真的执行，以及成本和耗时。这些问题分别涉及可观测性、运行控制和评测。

Agent Harness 是使模型能持续工作的外围运行系统，包括工具编排、上下文、状态、权限和停止处理；evaluation harness 是运行评测任务、隔离环境、收集结果并评分的设施。它们可能共享组件，但不是同义词。Trace 是一次运行的可观察轨迹，Eval 是具体任务及评分办法，Baseline 是用于比较的参考版本或成绩。给系统加日志并不等于具备评测能力。

教学案例是修复一个失败测试。先固定初始仓库和失败输入，再让 Agent 修改；用外部测试确认功能，检查它有没有删除测试或改掉验收条件。除了成功率，还要看耗时、token 成本和危险动作。自然语言自评可辅助解释，但不能替代可执行断言；评估者和生成者分开也不保证评估者一定正确。

NaumiAgent 的 harness 目录同时承担运行完成门、证据、评测、重放等职责。学习时应按职责看具体类型，而不要把所有可靠性机制都归结为一个“万能 Harness”。检查器能验证明确规则，无法自动判断所有业务意图；未定义清楚的验收条件仍然会导致错误结论。

## 把机制走一遍

教学数据：基线 20 次成功 14 次，新版本 20 次成功 16 次，观测提升是 10 个百分点，不等于已证明普遍优于基线。应使用相同任务、初始环境、预算与评分器，重复运行并看不确定性；保留失败题和安全指标，防止只优化平均分。

## NaumiAgent 源码走读与边界

[CompletionGateInput、HarnessRunState](../../src/naumi_agent/harness/completion.py)：完成门输入包含 changed_paths、checks、evidence、pending_todo_ids、known_failure_ids 和 infrastructure_errors；证据可以绑定 criterion_ids。HarnessRunState 为临时状态，具体持久化需看 Store。该接口让检查可计算，但“有回执”只证明记录的检查发生，不能保证检查覆盖了全部用户要求。

[compare_eval_repetitions](../../src/naumi_agent/harness/eval_statistics.py)：重复评测比较有专门统计逻辑；测试使用构造的结果验证统计行为。它不是一份已经证明 NaumiAgent 线上成功率提高的实验报告。

对应测试：[test_harness_eval_statistics.py](../../tests/unit/test_harness_eval_statistics.py)、[test_todo_reconciliation.py](../../tests/unit/test_todo_reconciliation.py)。阅读函数与断言后再运行；测试文件的存在本身不能证明生产可用。

## 面试陪练：从概念到追问

### 1. 概念题：Harness、Trace、Eval 分别是什么？

考察意图：概念层级是否正确。

回答顺序：结论 → 工作机制 → 本章项目证据 → 适用边界。

示范回答：Agent Harness 承载模型运行；评测 Harness 批量执行受控题目并评分；Trace 是运行轨迹，Eval 是题目和判定规则。两类 Harness 可以复用工具和记录组件。NaumiAgent 把多个职责放在 harness 目录，不能因此把这些概念当同义词。

继续追问：日志就是 Trace？→日志需关联 run/call 才易还原轨迹；有 Trace 就有成功标准？→没有。

常见失分：把 Harness 定义为日志加测试的固定三件套。

证据入口：[CompletionGateInput、HarnessRunState](../../src/naumi_agent/harness/completion.py)；设计类回答是教学方案，不能当成现有产品保证。

### 2. 设计题：如何评估一个代码 Agent？

考察意图：能否从任务得到指标。

回答顺序：结论 → 工作机制 → 本章项目证据 → 适用边界。

示范回答：固定仓库起点和目标缺陷，验证原失败测试以及回归，检查没有删测试或硬编码答案。同时记录成本、耗时和越权动作。开放式质量可加入人评或校准的 judge，但应保留环境断言和反例，防止模型自夸。

继续追问：怎么防测试泄漏？→隔离最终评测集；第三方依赖挂了？→归为环境失败并记录。

常见失分：只用最终回答与参考答案相似度打分。

证据入口：[CompletionGateInput、HarnessRunState](../../src/naumi_agent/harness/completion.py)；设计类回答是教学方案，不能当成现有产品保证。

### 3. 项目题：完成门怎样约束模型说完成？

考察意图：代码与效果的对应。

回答顺序：结论 → 工作机制 → 本章项目证据 → 适用边界。

示范回答：CompletionGateInput 接收文件变化、检查和证据，还区分基础设施错误。engine.run 对 prepared completed 再检查，可能改为未验证或 blocked。它比纯文本结束强，但强度受验收合同影响；漏掉的要求不会自动被发现。

继续追问：没有检查可用？→明确未验证；回执和产物对不上？→需绑定版本或指纹。

常见失分：把 receipt 当作万能正确性证明。

证据入口：[CompletionGateInput、HarnessRunState](../../src/naumi_agent/harness/completion.py)；设计类回答是教学方案，不能当成现有产品保证。

### 4. 故障题：新版本得分提高，为什么还可能不能发布？

考察意图：理解统计与代理指标。

回答顺序：结论 → 工作机制 → 本章项目证据 → 适用边界。

示范回答：可能是样本少、环境变化、评分器偏差，或平均分提高但安全退化。要比较同条件重复试验，报告样本数和不确定性，并设安全阻断指标。若模型只学会钻 grader 漏洞，分数提高不代表业务成功。

继续追问：judge 与人意见相反？→抽样校准并审查标准；只有一次成功？→作为案例不作普遍提升结论。

常见失分：把一次演示或少量分数提升写成线上收益。

证据入口：[CompletionGateInput、HarnessRunState](../../src/naumi_agent/harness/completion.py)；设计类回答是教学方案，不能当成现有产品保证。

### 5. 取舍题：什么时候用规则评分，什么时候用模型评分？

考察意图：挑选合适判定方法。

回答顺序：结论 → 工作机制 → 本章项目证据 → 适用边界。

示范回答：文件存在、测试退出码、订单状态适合规则检查；表达质量和完整性可用模型或人，但要制定可解释评分表并校准。多个 judge 投票也可能共同偏误。取舍看验证对象，不能因为模型方便就舍弃业务数据库。

继续追问：评价中文报告？→事实引用与表达分开评分；成本高怎么办？→分层抽样和回归集。

常见失分：认为换一个模型评分就天然独立可靠。

证据入口：[CompletionGateInput、HarnessRunState](../../src/naumi_agent/harness/completion.py)；设计类回答是教学方案，不能当成现有产品保证。

## 动手练习、白板题与验收

为代码修复写四项断言：原失败用例通过、回归通过、不删除测试、产物可运行。手算 14/20 与 16/20，再检查统计测试的输入、delta 与 verdict。注明这是教学数据。

仓库根目录运行（需已完成项目开发环境安装）：

```bash
.venv/bin/python -m pytest tests/unit/test_harness_eval_statistics.py -q
```

白板评分：四项断言各 1 分；区分百分点提升与统计证据 1 分。 本章实验范围与本轮实际执行结果见[验证记录](18-评审与验证记录.md)。

## 学完怎样写进简历

只有阅读时写“学习并复现本章测试，能解释上述边界”；只有亲自实现并验证后才能写“设计/实现”。用你的实际负责范围、实验条件和结果替换描述，不得把教材项目整体归为个人独立开发。

合上文档自测：能否用周报或代码修复场景解释本章概念、画出关键数据流，并回答第 4 题的两个追问？说不清时回到源码走读，记录一个需要进一步验证的假设。

## 延伸阅读

[Anthropic：Agent 与 evaluation harness 的定义、轨迹和环境终态](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)。
