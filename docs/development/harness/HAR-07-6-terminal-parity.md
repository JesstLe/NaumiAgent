# HAR-07.6 New UI / TUI Harness 回执与详情字段一致性

## 用户结果

Harness 运行结束后，New UI 与 TUI fallback 都只显示一张完成回执。回执把通用运行结果与
权威 Harness Receipt 合并展示；进入详情后，两端都可查看协议允许的完整有界
Criterion、Finding、Check、Evidence、Replay Difference、Artifact 与 Anomaly 字段，不再因
渲染器内部的固定 `slice()` 永久丢失后续记录。

## 权威边界

- 数据权威仍是已持久化的 `harness/receipt`、`harness/explain`、`harness/replay`；前端只投影，
  不重新推断成功、失败或验证状态。
- `tests/fixtures/har07/terminal-parity-golden.json` 是两个终端表面的共享 golden。Node 测试先让
  fixture 通过真实 protocol normalizer，再交给 renderer，避免用不合法样例制造虚假一致性。
- TUI 按 `run_id` 暂存先到达的 `harness_completion_receipt`，在同 run 的通用
  `completion_receipt` 到达时合并并消费，因而不会出现两张完成卡片。
- 两端继续遵守协议数量上限：Criterion 100、Finding 20、Check 50、Evidence 100、
  Difference 50、Artifact 100、Timeline 200。页面视口保持有界，超出当前高度的合法内容通过
  滚动访问，而不是在构建逻辑行时被截断。

## 字段与降级规则

| 区域 | 必须可见字段 | 降级行为 |
| --- | --- | --- |
| Compact Receipt | Harness 状态、检查/准则/证据计数、失败或基础设施检查、警告 | 无 Harness 同伴时保持原通用回执 |
| Criterion | id、description、status、evidence 数 | 非数组输入按空集合处理 |
| Finding | failure class、source、message、next step | 缺失可选文本时不生成伪值 |
| Evidence | id、kind、status、digest prefix、URI | 仅消费协议白名单字段 |
| Replay | status、anomaly 值、difference、artifact、timeline 数 | lookup 不可用时显示类型化 message |

成功/已验证使用绿色，未验证/变化/基础设施异常使用黄色，失败与摘要不一致使用红色；颜色只是
辅助信号，中文状态文字始终保留。Replay Artifact 的合法 `verified` 状态在两端都显示为
“已验证”。

## 验收证据

- Python 共享 golden 覆盖 TUI 合并回执、全部详情字段、畸形集合降级和敏感占位文本不泄漏。
- Textual 真实 app 测试通过 engine sink 依次投递 Harness 与通用回执，断言只调用一次完成回执
  formatter，且消费后的暂存状态为空。
- Node 共享 golden 通过真实协议归一化，覆盖第三条 Finding、第五条 Evidence、第四条
  Difference、第五条 Artifact、具体 Anomaly 值及行宽上限。
- 相关既有回执/详情测试继续覆盖无 Harness 同伴、窄屏和 lookup unavailable 场景。

## 当前不足

- HAR-07.4b 的断线重连 revision/gap 自动补发尚未实现；本模块不把进程内暂存当作持久恢复。
- HAR-07.5 的 `e/r/v`、复制回执和详情焦点交互尚未实现。
- TUI 当前是单前台运行模型；暂存映射用于对齐事件顺序，不宣称支持并发多 run 展示。
