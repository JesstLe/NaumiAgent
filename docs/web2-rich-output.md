# Web2 富内容输出

## 目标与进度

2026-09-11：将消息正文扩展为 Markdown、LaTeX、图片、指标卡片、数据表、图表和隔离运行的 HTML 交互组件。沿用现有浅色聊天布局与消息持久化，不修改历史执行时间线。

### 模块一：正文渲染（已验证）

采用 react-markdown、GFM、remark-math、KaTeX 和语法高亮；支持表格、任务列表、引用、链接、代码复制下载、图片预览。原始 HTML 不直接进入父页面 DOM。用户输入继续显示原文，助手回复使用富渲染。

验证：4 项正文单测通过，生产构建通过，Markdown/LaTeX 页面及刷新测试通过。实际使用 Kimi 返回包含表格和公式的回复，保存到 SQLite 会话 `97e3d5b4a2e8`，由现有 8765 API 读取并在 Web2 渲染、刷新验收通过（模型调用使用新配置的独立 Router，既有 API 进程未重启）。截图 `.naumi/data/rich-kimi-real.png`。现有移动端左栏会覆盖正文，已交由并行布局任务处理；生产包存在大 chunk 提示，后续组件按需加载。

### 模块二：富组件（已验证）

消息使用 fenced `naumi` JSON，version=1。类型包括 metrics、table、chart、image、html、tabs。类型/数据量/嵌套/数值均有校验；无效或流式未完成的内容保留原文入口。支持 CSV 导出（处理单元格公式注入）、搜索排序分页及当前筛选数据的数值统计。图表为真实 SVG，支持负值、空值、系列开关、图表类型切换及键盘/鼠标数值查看。

HTML 由用户点击运行，iframe sandbox 仅 allow-scripts，没有同源、顶层导航、弹窗权限；CSP 禁止 fetch、外部脚本、外部资源及表单提交。允许内联脚本、样式与 data/blob 图片。提供停止与重置。组件交互状态仅在当前组件实例中保留，刷新/切换标签后恢复初始状态；定义与输入数据随消息持久化。不宣称任意 HTML 脚本具有资源消耗上限。

真实 Kimi 根据 `git ls-files` 测得的前六种文件类型数量，生成 4-tab 组件，SQLite 会话 `0e7cc91303d4`，原始证据 `.naumi/data/rich-widget-real.json`。10 项前端定向测试通过。完整协议放在 `tools/output_protocol.py` 按需提供，系统提示词只保留简短入口，保持现有长度预算。

生产构建和浏览器交互验收通过：搜索、CSV 下载、折线切换、HTML 计数器运行/重置、父页面访问隔离、刷新恢复；真实 Kimi 组件的表格/图表/滑块估算值变化也已验证。13 项 system_prompt 回归通过。截图 `.naumi/data/rich-kimi-widget-real.png`。

示例（假设数据）：

````text
```naumi
{"version":1,"type":"chart","title":"订单趋势示例","source":"假设数据","x":"day","series":[{"key":"orders","label":"订单数"}],"rows":[{"day":"周一","orders":12},{"day":"周二","orders":18}],"style":"bar"}
```
````

### 模块三：生成与返回文件（待实现）

模型使用真实文件工具生成图像或分析结果，再通过受控资源发布返回可访问链接。须验证文件访问边界、持久化和浏览器显示，不能将图片占位符当作生成结果。
