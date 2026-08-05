# CC-03.1a 核心行为清单与证据校验

## 目标

为 Task、Permission、Doctor 三条首批迁入链路建立机器可读、失败关闭的行为清单。清单只记录
源码路径、声明符号、Naumi 目标路径、目标测试和差异决策，不保存或复制 Claude Code 的源码正文。

本切片是 CC-03.1 的最小前置，不是完整组件迁入。它回答四个问题：

1. 观察的是哪个固定 Claude Code commit；
2. 该源码路径是否允许以 `reimplement` 模式研究；
3. Naumi 的独立实现和测试位于哪里；
4. 行为是已对齐、Naumi 扩展，还是明确延期。

## 交付物

- `frontend/terminal-ui/cc-behavior-inventory.v1.json`
  - 绑定 v2 source identity 和 license scope digest；
  - 固定 `intake_mode=reimplement`；
  - 覆盖 Task、Permission、Doctor；
  - 每条 aligned 行为同时绑定 source symbol、target symbol 和 target test；
  - Naumi 特有行为必须写 `divergence`，不得伪装成 Claude Code 对齐。
- `src/naumi_agent/claude_source/behavior_inventory.py`
  - 使用严格 Pydantic schema；
  - 先校验 source identity 与 license scope；
  - 对每个源码路径重新执行 scope rule 匹配；
  - 只扫描声明符号和测试名，不将源码内容写入报告；
  - 拒绝绝对路径、父目录逃逸、符号缺失、测试缺失、symlink 逃逸和超大证据文件；
  - 输出确定性 digest、计数和稳定 finding code。
- `tests/unit/test_claude_source_behavior_inventory.py`
  - 临时真实 Git source fixture；
  - 正常、source stale、target/test anchor 缺失、schema 失败关闭、CLI 只读；
  - 本机受治理 Claude Code checkout 的真实验证。

## 当前行为范围

| 行为 | 决策 | Source | Naumi target |
| --- | --- | --- | --- |
| task status presentation | aligned | `TaskListV2`, `RECENT_COMPLETED_TTL_MS` | typed task snapshot + status renderer |
| task detail/cancel | Naumi extension | 无对应 source anchor | Bridge 驱动的详情与取消 |
| permission policy explanation | aligned | `PermissionRuleExplanation`, `stringsForDecisionReason` | Python 权威 permission snapshot |
| permission loading/empty/unavailable | Naumi extension | 无对应 source anchor | New UI permission center |
| doctor diagnostics loading | aligned | `Doctor` | typed Doctor Health page |
| doctor export bundle | Naumi extension | 无对应 source anchor | 有界脱敏预览与原子写入 |

`naumi_extension` 不代表 Claude Code 缺少类似产品能力，只表示本次固定源码锚点中没有建立可审计的
source→target 对齐关系。后续发现可信 source anchor 时，应通过人工审查修改清单，不能静默改变决策。

## 校验命令

```bash
python3 -m naumi_agent.claude_source.behavior_inventory \
  --inventory frontend/terminal-ui/cc-behavior-inventory.v1.json \
  --identity frontend/terminal-ui/cc-source-map.v2.json \
  --license-scope frontend/terminal-ui/cc-license-scope.v1.json \
  --source-root /Users/lv/Workspace/claude-code \
  --project-root .
```

成功报告必须满足：

- `status=valid`；
- `behavior_count=6`；
- `aligned_count=3`；
- `extension_count=3`；
- source、target、target test 的 verified count 与清单声明一致；
- Claude Code checkout 在校验前后保持只读、工作树状态不变。

## 验收标准

- 修改 source commit、identity digest 或 license scope digest 后报告 `stale`；
- source scope 不允许 `reimplement` 时报告 `invalid`；
- 任一声明符号或目标测试被重命名时报告 `invalid`；
- 三个 area 任一缺失或任一 area 没有 aligned 行为时 schema 拒绝加载；
- aligned、deferred、naumi_extension 的 evidence 组合不能互相伪装；
- 报告不包含源码片段、用户目录绝对路径、模型输入、密钥或原始诊断内容；
- 校验器不修改 Claude Code checkout 或 Naumi target 文件。

## 自我审视与未完成项

- 已做到真实静态锚点验证，不是 Prompt 套壳；但符号存在只证明结构证据仍在，不证明完整语义等价。
- 当前只覆盖六条核心行为，不包含 CC-03.1 规划中的完整键位、焦点、loading、empty、error、
  detail、cancel 矩阵。
- 当前 target test 证明 Naumi 行为存在，但 source-like fixture 与真实 Bridge 共用 golden 仍属于 CC-03.5。
- 当前没有把行为 inventory 接入 CC-05 upstream diff；source 更新只会因 identity binding 变为 stale，
  尚不会生成逐行为差异。
- CC-03.1b 已补齐三条链路的完整 source state/keyboard/focus/error 矩阵；后续进入 CC-03.2 protocol
  字段语义映射，详见 `CC-03-1b-complete-behavior-matrix.md`。
