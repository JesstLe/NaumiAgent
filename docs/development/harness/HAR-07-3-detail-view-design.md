# HAR-07.3 Harness 运行详情视图

## 用户结果

用户可在新 UI 输入 `/harness detail [run-id|latest]` 打开全屏详情页，也可在 TUI fallback
执行同一命令获得 Markdown 详情。两种表面只展示 Harness Store 经 Explain/Replay 类型协议提供的
权威事实，不解析模型自然语言，也不读取 Artifact 正文。

## 权威字段

| 分区 | 来源 | 字段 |
| --- | --- | --- |
| 概览 | Explain | objective、status、summary |
| 准则 | Explain | criterion id、description、status、evidence ids |
| 失败分类 | Explain | failure classes、findings、next step |
| 检查 | Explain | check id、status、duration |
| 证据 | Explain | evidence id、kind、status、digest prefix、URI |
| Replay | Replay | status、anomalies、timeline count |
| 差异 | Replay | field、baseline、current |
| Artifact | Replay | id、kind、reference、status |

Explain 的 criterion 是 HAR-07.3 新增的类型化投影：由持久化 completion contract 机械映射，
Python serializer 与 Node normalizer 均限制为 100 项，描述限制 500 字符，单项 evidence ids
限制 100 项。其余集合沿用 HAR-07.1b 的白名单与数量上限。

## 新 UI 状态机

1. 无参数或 `latest` 从当前时间线最后一张完成回执取得精确 run id；没有回执时仅显示可行动警告，
   不发后端请求。
2. 打开 `harness_detail` 瞬态路由并保存对话滚动锚点，同时发出精确 run id 的
   `harness/explain/request` 与 `harness/replay/request`。
3. 两类响应分别进入 revision 幂等缓存；仅与当前 run id 匹配的响应结束相应 loading 状态。
4. 页面从缓存派生，不复制或改写权威数据。上下键、PageUp/PageDown、Home/End 滚动，Esc
   恢复原对话锚点。
5. 显式会话恢复关闭详情路由并清空其瞬态选择；普通 Workbench 路由的既有恢复语义不受影响。

## 降级与视觉语义

- 绿色：已验证、满足、通过、已记录、已复现。
- 黄色：未验证、未满足、发生变化、加载或数据暂不可用。
- 红色：检查失败、Artifact 缺失或摘要不一致。
- ANSI 关闭后仍保留完整中文状态文字，不以颜色作为唯一信息通道。
- Explain 与 Replay 独立加载、独立失败；一个分区不可用不会伪造另一分区成功。
- 80、120、200 列均按中文显示宽度折行，并严格裁剪到当前视口高度。

## TUI fallback

`/harness detail` 先 Explain；成功时从结果取得精确 run id，再对同一运行执行安全 Replay，避免
`latest` 在两次查询间漂移。输出由共享白名单投影生成，字段分区与新 UI 相同。该命令只读，
不执行模型、工具、Harness check 或原任务。

## 验收证据

- Python 单元测试覆盖字段上限、所有分区、not_found/unavailable 和禁止伪造成功。
- Node 测试覆盖协议白名单、独立 loading、路由与精确请求、滚动锚点及 80/120/200 列边界。
- 真实集成测试使用 SQLite Harness Store、新 Service、JSONL Bridge、Node normalizer/reducer 和
  页面 renderer，证明同一持久化运行可完整显示且不会越界。

## 明确不在本切片

- HAR-07.4b 的断线 revision/gap 自动补发。
- HAR-07.5 的完成卡 `e/r/v` 快捷键、Evidence 单项页和复制操作。
- HAR-07.6 的跨表面结构化 snapshot parity；本切片先建立字段集合与共享投影边界。
