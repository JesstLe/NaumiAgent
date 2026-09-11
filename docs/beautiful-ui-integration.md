# Beautiful UI 接入与样式核验

2026-09-11。官方来源：https://github.com/slev12397/beautiful-ui ，版本 `ff0f74d62d8be9d89bcb735b3632e31a6ccf88dc`。MIT，Copyright (c) 2026 Shane Levine。

## 样式接入

已用官方组件源码替换此前自行重写的组件样式。`frontend/web2/src/beautiful/upstream/foundation.css` 来自官方完整 `app/globals.css`，保留 tokens、`@theme`、primitive spacing、keyframes、reduced-motion 和组件专用规则。官网 registry 的 foundation 切片存在孤立大括号，因此没有使用该截断版本。

`frontend/web2/scripts/build-beautiful-ui.mjs` 使用 Tailwind 编译官方 className，展开级联层并将选择器限定在 `.bui-root`，生成 `upstream.generated.css`；开发启动、生产构建自动运行。`beautiful.css` 仅负责宿主位置、字体及空状态，Web2 的通用 button／svg／focus／placeholder 规则排除组件区域。

字体使用本地包 Inter Variable、JetBrains Mono Variable；阴影使用官方 `shadow-plugin/unprefixed`。原始文件 SHA256、上游版本及本地补丁说明记录在 `upstream/provenance.json`，许可证在 `beautiful/LICENSE`。官网 Inter 来自 Next font，本地来自 Fontsource，字体名称与中文 fallback 不同。

## 21 类组件的决定

维持 Codex 的页面层级，`web2` 与 `web` 同级。组件进入已有对话、顶部摘要和内容区，公共状态和转换保留在 `frontend/shared`。

| 官方组件 | 位置或原因 | 结果 |
| --- | --- | --- |
| Loading State | 已有连接、加载、执行反馈 | 暂缓，等用户决定 |
| Thinking | 对话内折叠执行过程；状态来自真实运行 | 已引入 |
| Streaming Text | 已有流式回复 | 暂缓，等用户决定 |
| Approval Card | 已有真实权限确认 | 暂缓，等用户决定 |
| Tool Chips | 执行过程中的工具条目；展开查看记录 | 已引入 |
| Task Rows | 已有 Todo 清单 | 暂缓，等用户决定 |
| Chat | 已有 Codex 对话区 | 暂缓，等用户决定 |
| Prompt Bar | 已有模型、附件、命令、发送；语音需接口 | 暂缓，等用户决定 |
| Recommendation Card | 与现有权限／提案审查相似 | 暂缓，等用户决定 |
| Context Cards | 顶部摘要「上下文」；健康、依据、来源和时间 | 已引入 |
| Diff Table | 按用户要求保留现有右侧彩色 Diff | 暂缓，等用户决定 |
| Records Table | 已有验证记录视图；原版 AI 属性计算尚无对应业务接口 | 暂缓，等用户决定 |
| Filter Table | 与现有任务筛选相似 | 暂缓，等用户决定 |
| Sidebar Nav | 保留当前 Codex 导航 | 暂缓，等用户决定 |
| Search | 已有会话搜索、命令补全 | 暂缓，等用户决定 |
| Flowchart | 顶部摘要「依赖」；真实 Todo 依赖、拖动与连接高亮 | 已引入 |
| Insight Cards | 顶部摘要「洞察」；任务／验证／上下文分布与轮播 | 已引入 |
| Code Block | 已有代码围栏与行号 | 暂缓，等用户决定 |
| Fine-tune Card | 已有设置页；当前没有原版设计属性编辑对象 | 暂缓，等用户决定 |
| Selection Actions | 选中助手回答时出现；预设／自定义操作追加到共同草稿 | 已引入 |
| Agent Screen | 尚无真实屏幕流、录制和控制接口 | 暂缓，等接口及用户决定 |

合计引入 6 类，暂缓 15 类。

## 数据和交互边界

- Thinking／Tool Chips 保留原版闪光、入场、折叠、悬停箭头和详情过渡。移除演示定时成功，改由公共运行记录／工具事件驱动。工具区域不再次展示 Diff。过长标题截断并提供完整 tooltip；工具详情支持多行和滚动。执行结束后从共同后端读取已保存预览，旧记录在关联证据明确时从会话恢复，详见 [工具输出持久化修复](tool-output-persistence.md)。长输出仍受引擎事件预览长度限制。
- Selection Actions 保留原版胶囊、更多动作展开、输入宽度变化、图标和阴影，连接真实文字选择。解释、改写、精简、语气、语法和自定义指令只生成草稿，保留已有内容，不自动发送。小屏允许工具条内部横向滚动，页面滚动／Escape／换会话关闭工具条。
- Context Cards 使用真实快照，空数据不渲染官网样例；刷新失败保留旧内容，提示错误并可重新刷新。重复任务标题不会产生 React key 冲突。公共控制器以会话 generation 和请求 revision 防止旧请求覆盖新会话。
- Flowchart 使用 Todo 的 `blocked_by`，拓扑排序并保留循环节点；缺失依赖、循环及被循环阻塞的节点均显示提示。拖动只调整当前视图，不修改任务依赖，重开摘要会重置位置。
- Insight Cards 依据已加载记录计算占比，舍入后合计 100%；分段按钮可以检查不同分组，轮播与动作按钮可用。当前启用原版分布卡。官网收益对比／异常曲线缺乏对应真实时序数据，未将示例曲线当作业务结果；后续有真实序列后再接入。
- 官方源码中的默认样例仅保留作上游参考。生产包装层始终传入真实数据，空记录直接显示空状态。

## 验证证据与自查

- 官网与隔离挂载的原版 Context Cards 对比：背景、文字颜色、字号／字重／行高、10px 卡片圆角、10px 12px 标题内边距、全部阴影层、300ms 过渡和 `cubic-bezier(0.23, 1, 0.32, 1)` 一致。`fade-up` 动画一致。字体包来源／名称不同，不声称逐像素完全一致。详细结果见 `beautiful-ui-style-check.json`。
- 36 项前端单元测试通过，包含依赖排序、重复边、循环、自依赖、未知状态、百分比舍入与空值。最终全套 24 项浏览器测试通过，覆盖两种 Web、面板、Diff、斜杠命令、上下文刷新恢复、依赖拖动、统计、原版 CSS 和 390px 文字工具栏；生产构建通过。打包仍提示主 JS chunk 超过 500kB，后续可按组件拆包。
- 真实既有会话 `e1fe45644bc7`：读取运行记录、展开条目、选中回答追加草稿，再还原草稿，无页面异常。读取记录的数量随会话继续使用而变化。
- 独立本地验收会话 `8e7cb78f0416`：真实接口创建 2 个任务和 1 条依赖；实际 issue 未填验收标准，服务端计算出 `missing`，卡片显示「缺少验收标准」。验证了卡片、连线、统计轮播及返回上下文，390px 摘要 `scrollWidth = clientWidth = 390`，无页面异常。这是明确标记的本地验收数据，不是 Agent 业务完成结果。
- 本地截图：`.naumi/data/beautiful-real-context.png`、`beautiful-real-flow.png`、`beautiful-real-context-mobile.png`、`beautifului-real-trace.png`。原版样例只用于临时视觉核验，临时页面已删除，不随产品发布。
- Ruff 检查通过。单独运行 `tests/integration/test_workbench_api_smoke.py` 的两项真实 HTTP 合同测试通过。Python 全量收集到 6271 项，运行未正常结束，手动停止后未获得完整失败报告；全量不记为通过。本轮未修改 Python 业务代码。

当前限制：尚未启用的 15 类组件及洞察的时序曲线见上表；宿主布局和中文业务文案与官网画廊不同，原版演示中的模拟执行、预制改写和假数据不会作为真实能力启用。
