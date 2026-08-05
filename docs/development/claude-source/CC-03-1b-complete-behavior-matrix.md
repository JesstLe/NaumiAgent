# CC-03.1b Task/Permission/Doctor 完整行为矩阵

## 目标

在进入 CC-03.2 protocol 字段映射前，将 Task、Permission、Doctor 三条链路的八个行为维度完整登记为
`3 × 8 = 24` 个机器可校验单元格。矩阵记录行为结论和证据位置，不复制 Claude Code 源码正文，
也不把 Naumi 特有能力伪装成 source 对齐。

本切片回答：

1. 每个 area 的 loading、empty、error、detail、cancel、keyboard、focus、presentation 是否有结论；
2. 结论是 source→target 已对齐、Naumi 扩展、明确延期，还是产品边界上不适用；
3. 非 N/A 结论能否追踪到真实声明符号和目标测试；
4. 矩阵是否仍绑定 CC-03.1a、固定 source identity 与 license scope。

## 交付物

- `frontend/terminal-ui/cc-behavior-matrix.v1.json`
  - 精确包含 24 个、按 `cell_id` 排序且不重复的单元格；
  - 绑定 `cc-behavior-inventory.v1.json` 的内容摘要；
  - 每格包含有界单行行为描述和严格 disposition；
  - `not_applicable` 不得携带伪证据，必须说明产品边界。
- `src/naumi_agent/claude_source/behavior_matrix.py`
  - 严格 Pydantic schema，未知字段失败关闭；
  - 复用 CC-03.1a verifier 校验 source identity、license scope、路径、符号和测试；
  - 输出确定性 audit digest、disposition 计数和稳定 finding code；
  - source、target 和测试文件均只读。
- `tests/unit/test_claude_source_behavior_matrix.py`
  - 真实临时 Git source fixture；
  - 24 格覆盖、证据形状、core binding、缺失锚点和 CLI 只读验证；
  - 本机受治理 Claude Code checkout 的真实校验。

## 完整矩阵

| Area | cancel | detail | empty | error | focus | keyboard | loading | presentation |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Doctor | extension | aligned | extension | aligned | aligned | aligned | aligned | aligned |
| Permission | N/A | aligned | aligned | aligned | extension | extension | extension | aligned |
| Task | extension | extension | aligned | extension | extension | extension | extension | aligned |

统计固定为：

- `aligned = 12`；
- `naumi_extension = 11`；
- `deferred = 0`；
- `not_applicable = 1`。

Permission cancel 的 N/A 只适用于“权限策略中心”：该页面是只读策略与历史视图。待决权限请求的拒绝仍由
结构化 interaction/permission 协议负责，不能错误合并为策略中心的 cancel。

## 证据规则

| Disposition | Source evidence | Target evidence | Target test | Rationale |
| --- | --- | --- | --- | --- |
| `aligned` | 必须 | 必须 | 必须 | 必须为空 |
| `naumi_extension` | 必须为空 | 必须 | 必须 | 必须说明差异 |
| `deferred` | 必须 | 必须为空 | 必须为空 | 必须说明延期边界 |
| `not_applicable` | 必须为空 | 必须为空 | 必须为空 | 必须说明产品边界 |

证据列表必须排序且不得重复；路径必须为安全 POSIX 相对路径。Verifier 只判断声明和测试锚点，报告中不输出
源码片段、绝对用户目录、原始异常、模型输入或密钥。

## 校验命令

```bash
python3 -m naumi_agent.claude_source.behavior_matrix \
  --matrix frontend/terminal-ui/cc-behavior-matrix.v1.json \
  --core-inventory frontend/terminal-ui/cc-behavior-inventory.v1.json \
  --identity frontend/terminal-ui/cc-source-map.v2.json \
  --license-scope frontend/terminal-ui/cc-license-scope.v1.json \
  --source-root /Users/lv/Workspace/claude-code \
  --project-root .
```

成功报告必须满足：

- `status=valid`、`cell_count=24`；
- disposition 计数与上表一致；
- source、target、target test 的 verified count 等于矩阵声明数；
- core inventory、source identity、license scope 任一摘要变化时失败关闭；
- Claude Code checkout 与 Naumi target 在校验前后保持不变。

## 验收标准

- 删除、重复或打乱任一 cell 后 schema 拒绝加载；
- 每个 area 至少存在一个真正的 aligned cell；
- aligned 缺少任一侧证据、extension 携带 source 或 N/A 携带证据时 schema 拒绝；
- core inventory 摘要不匹配时报告 `stale/core_inventory_binding_mismatch`；
- source/target symbol 或 target test 缺失时报告 `invalid`；
- verifier 对同一输入重复运行产生相同 audit；
- 使用真实受治理 checkout 完成一次端到端只读校验。

## 自我审视与未完成项

- 本切片证明 24 个行为格都有明确、可追踪的工程结论，但“符号仍存在”不能单独证明视觉和交互完全等价。
- 多个 source state 当前绑定同一组件声明；更细的字段和值域对应关系属于 CC-03.2，不能在本矩阵中猜测。
- `deferred=0` 表示当前范围没有只观察 source 却完全缺失 target 的行为，不表示 CC-03 已完成。
- source-like fixture 与真实 Bridge fixture 共用 golden、无色彩/窄屏/TUI 审计、上游行为影响路由仍分别属于
  CC-03.5、CC-03.6 和 CC-05。
- CC-03.2a 已完成三个 Bridge snapshot 的字段和值域映射；下一小切片是 CC-03.2b UI-local 状态映射，
  详见 `CC-03-2a-snapshot-semantic-mapping.md`。
