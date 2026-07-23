# HAR-07.5b Harness Evidence 焦点

## 问题

HAR-07.3 已让用户查看 Explain、Replay、准则、检查、证据、差异和 Artifact，但长运行的详情页会让证据记录
分散在大量信息之间。用户无法快速回答三个关键问题：某条证据支持哪个准则、关联了哪个失败发现，以及是否有
证据引用缺口。此前文档约定的 `v` Evidence 交互仍未实现。

## Authority 与投影边界

- 唯一事实来源仍是持久化 Harness Run 经 `HarnessService.explain_run()` 生成的类型化
  `harness/explain`；本切片不增加 Evidence Store、不读取 artifact body，也不执行模型、工具或检查。
- Evidence 焦点只做确定性的 ID 关联：反向索引 criterion/finding 的 `evidence_ids`，不重新分类失败，
  不推断 evidence 是否足以证明准则。
- 权威 evidence 列表中没有被 criterion/finding 引用的记录显示“未被准则或发现引用”；引用 ID 不存在于
  权威 evidence 列表时显示“引用存在但权威证据记录缺失”。两种情况不能混为一类。
- 继续沿用 Explain 协议的白名单、集合上限和 500 字符公开文本限制。重复引用在同一关系中去重，顺序稳定。

## 用户交互

### New UI

- `/harness evidence [run-id|latest]` 直接打开 Evidence 焦点，只请求 Explain，不为不可见的 Replay 发请求；
- 在完整 `/harness detail` 中按 `v` 切换到 Evidence 焦点，再按 `v` 返回全部详情；
- 完整详情和 Evidence 焦点分别保存滚动位置，切换不会让用户丢失阅读锚点；
- 从 Evidence 焦点第一次返回全部详情时，若 Replay 尚未缓存，才按精确 run id 请求 Replay；
- `e` 继续显式刷新 Explain，Esc 返回原对话滚动锚点；session replay 恢复到默认完整详情状态。

### CLI / Textual TUI

- `/harness evidence [run-id|latest]` 通过共享 slash router 调用相同 `HarnessService`，渲染同一公开字段集合；
- TUI 不解析 New UI ANSI，也不复制失败分类逻辑；它使用共享 Python Evidence Markdown 投影；
- not found/unavailable 保留原始类型化状态文案，不伪造“已验证”或空成功结果。

## 展示字段

Evidence 焦点保留：run id、运行状态、目标、摘要；每条 Evidence 的 id、kind、status、digest prefix、URI；
关联 criterion 的 id、description、status；关联 finding 的 failure class、source、message、next step 和 check ids。
无色模式下仍可依赖“准则 / 发现 / 关联 / 引用缺口”等文字标签理解状态。

## 验收证据

- 共享 HAR-07 golden fixture 同时驱动 Python 与 Node，验证关联准则、失败发现、孤立 Evidence 和缺失引用；
- New UI 在 80/120/200 列下不溢出，Evidence 焦点不渲染 Replay/Artifact；
- `v` 往返保留两个独立滚动位置，缺少 Replay 时只补发一次，重复切换不制造请求；
- `/harness evidence latest` 通过真实 SQLite Harness Store、重建后的 Service 和共享 slash router；
- unavailable/not found 不出现伪造的已验证状态，原始工具输出和非白名单字段不进入投影。

## 未完成边界

本切片本身不实现完成卡直接进入详情、系统剪贴板复制、artifact body 预览或 HAR-07.4b reconnect gap
recovery。系统剪贴板复制后来由 HAR-07.5c1 独立完成；其余边界继续由 HAR-07.5c2+ 与
ARC-02.5/HAR-07.4b 跟进，不能因 Evidence 焦点完成而把 HAR-07 标记为 implemented。
