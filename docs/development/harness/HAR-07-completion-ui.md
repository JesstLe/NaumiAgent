# HAR-07 Completion Receipt UI 与恢复

## 目标

让新 UI 和 TUI fallback 都以同一权威 Harness Receipt 展示完成状态，并可进入 Explain、
Evidence、Check 和 Replay 详情，而不是从模型自然语言猜测结果。

## 子模块

| ID | 子模块 | 用户结果 |
| --- | --- | --- |
| HAR-07.1 | Protocol message | `harness/receipt`, `harness/explain`, `harness/replay` |
| HAR-07.2 | Compact card | 状态、耗时、检查、证据、风险、警告 |
| HAR-07.3 | Detail view | criteria/check/evidence/failure classification 分区 |
| HAR-07.4 | Recovery | resume/reconnect 后按 revision 补发且幂等 |
| HAR-07.5 | Interaction | `e` explain、`r` replay、`v` evidence、复制回执 |
| HAR-07.6 | TUI parity | Textual 表面语义一致，布局可降级 |

## 视觉语义

- verified 使用绿色；unverified 黄色；blocked/失败红色；基础设施问题黄色而非伪装测试失败。
- Git additions/deletions 分别绿色/红色；未跟踪、恢复、警告使用独立语义色。
- 窄屏先保留状态、失败分类和下一步，再裁剪次要 digest/时间。

## 验收标准

- Receipt 先持久化后发事件；丢包用 request/revision 补齐，不产生两张卡。
- 运行完成、部分完成、取消、权限拒绝、Store 故障五种真实场景可区分。
- `/resume` 后 card 与关闭前一致，但瞬态 focus/sidebar 回到默认状态。
- 80/120/200 列和中文宽字符下无溢出，色彩关闭时仍可仅凭文字区分。
- New UI 与 TUI snapshot 的字段集合相同。
- A3：真实 Bridge 进程和新的 Store 实例恢复回执、Explain 与 Replay。

## 非目标

不在前端重新分类失败，不允许 UI 改写 Receipt。
