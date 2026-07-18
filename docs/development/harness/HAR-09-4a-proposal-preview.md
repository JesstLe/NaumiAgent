# HAR-09.4a Evolution Proposal Preview v1

## 目标

把达到 `review_ready` 的 Evolution Candidate 转换为可解释、可验证、不可执行的 Proposal Preview。
Preview 是 HAR-09.4 的生成结果，也是 HAR-09.5 持久 Review Queue 的输入契约；本切片不把 Preview
写入 Workbench Store，不创建任务、Issue、worktree 或实验。

## 为什么先做 Preview

Candidate 表达“观察到了什么问题”，Proposal 表达“建议改变哪一类能力、影响范围是什么、如何机械
验证”。Workbench Proposal 当前要求 mission/task 绑定，并且尚未具备 Evolution source revision、
defer/merge 和 cooldown 语义。直接把 Candidate 塞进现有表会丢失来源一致性，也会混淆 HAR-09.4
生成与 HAR-09.5 入队两个治理阶段。

因此本切片先固定无副作用的 Preview 契约，下一切片再设计显式、幂等的 Workbench Queue 适配。

## 六类 Proposal

| 类型 | 典型来源 | 例子 |
|---|---|---|
| `knowledge` | knowledge scope 或 knowledge finding | 缺失、过时的运行知识 |
| `profile` | provider/model/profile scope 或环境契约 finding | 模型能力、上下文、Provider 参数 |
| `prompt` | prompt/instruction scope 或行为失败 | 过早结束、重复无进展 |
| `tool` | tool/MCP/browser scope 或工具契约失败 | 参数 schema、结果契约、调用能力 |
| `test` | tests/eval/verification scope 或验证 finding | 回归覆盖、flaky test、缺失测试 |
| `code` | 其他源码问题的安全兜底 | 正确性、可维护性实现缺陷 |

分类优先级为：明确 logical scope → 明确路径 → finding code → `code` fallback。每次分类同时输出稳定
`classification_reason`，禁止由 LLM 临时猜测类型。测试覆盖全部六类以及路径优先级。

## Source Snapshot 与稳定身份

Preview 保存：

- Candidate ID、Store revision 和完整 Candidate SHA-256；
- occurrence count、last observed time；
- aggregation policy 与趋势；
- generator version `evolution-proposal-v1`。

`proposal_id` 由上述不可变来源、Proposal 类型和 generator version 确定性哈希生成。相同 Candidate
revision 重复读取得到同一 ID；新增 Evidence 导致 revision/digest 改变时生成新 ID。Python producer
先复算 Candidate digest，Node consumer 再复算 Proposal ID，并核对 Candidate ID、revision、证据数、
scope 和 risk，防止传输层把旧 Preview 贴到新 Candidate 上。

## 验证计划

每个 Candidate expected metric 被转换为结构化验证步骤：

- `harness_replay`：同一 Harness 输入安全回放，比较失败分类与验收条件；
- `self_review_static`：同 scope 重跑静态扫描，比较同 finding 数量；
- `feedback_recurrence`：比较后续观察窗口的同根反馈复发率，不把沉默当作自动通过。

Preview 不发明具体测试命令，也不声称验证已经执行。命令、baseline 与预算将在 EVO-02 Experiment
Contract 和 HAR-08 Eval 中绑定。

## 安全不变量

- `needs_evidence`、protected scope 或 verifier hard block 不生成 Preview。
- `requires_human_review=true`、`executable=false`、`experiment_eligible=false`、`state=preview` 为固定值。
- Preview 不包含 workspace 绝对路径、用户原文、stdout、secret 或完整 Evidence payload。
- intended files 只从已验证的相对源码 scope 提取，逻辑 scope 不猜测文件。
- Preview 生成是纯计算，不读网络、不写 Candidate Store/Workbench Store/Git。
- bypass 不改变上述 authority contract。

## 双通道

- 用户：`/evolution detail <candidate-id>` 显示 Proposal 类型、ID、影响范围、目标文件和验证计划。
- Agent：`evolution_candidates(action='detail', ...)` 通过同一个 `EvolutionReviewService` 获取相同内容。
- New UI：typed `evolution/review` detail 严格校验并优先显示不可执行 Preview。
- Textual/legacy fallback：使用同一 Markdown renderer，不复制生成逻辑。

## 验收标准

- 六类建议均有确定性 classification fixture。
- 同 revision 重复生成 ID 相同，新 revision ID 不同。
- Candidate digest 被篡改时拒绝生成。
- 单次直接反馈、Agent-only、protected scope 或 verifier block 不生成 Preview。
- validation plan 与 Candidate expected metrics 一一对应且 verifier 受支持。
- `/evolution detail`、Agent Tool、Python typed payload、Bridge 和 Node renderer 内容一致。
- payload 把 `executable=true`、`experiment_eligible=true`、错误 source revision/digest 或错误 ID 视为协议错误。
- 生成前后 Candidate audit event 数量不变，Workbench Store/Git 无写入。

## 后续：HAR-09.5a

下一切片需为 Preview 设计持久 Workbench Queue Adapter：

1. 显式 enqueue，禁止 detail 自动入队；
2. 以 proposal ID + Candidate revision 幂等；
3. 保存 source provenance，不把它编码进 title/questions；
4. 支持 approve/reject/defer/merge 和冷却期；
5. 只有 approved Proposal 才能进入 EVO-02 Experiment Contract；
6. 所有动作产生审计事件，bypass 仍不跳过记录。
