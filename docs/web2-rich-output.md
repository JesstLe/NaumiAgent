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

### 模块三：生成与返回文件（已验证）

新增 `output_publish` 工具及共享 `/output [文件路径]` 命令，CLI/TUI/Web 均使用同一执行入口，工具已注册 Engine 与权限目录。无参数返回完整中文格式帮助；svg 参数创建真实矢量文件；path 发布工作区内已有 SVG/PNG/JPG/WEBP/GIF/PDF/CSV/JSON/TXT/MD。文件复制到会话库同目录下的 output-assets，以 SHA-256 命名，原文件删除后仍可显示。图片原地预览/放大，文档与数据文件下载。

资源通过带鉴权的 `/api/v1/output-assets/{hash.ext}` 返回。前端用现有 API 令牌请求 Blob，不将令牌写入 URL；切换消息后清理 Blob URL。文件访问校验工作区边界，资源接口禁止目录穿越/符号链接。SVG 检查静态元素与外部引用，栅格图使用 Pillow 验证格式、尺寸、动画帧数。每文件最大 20 MB、SVG 最大 500 KB；每会话最多调用 50 次。并发发布同一内容采用原子硬链接，避免 Windows 覆盖竞争。

真实验收：Kimi 实际发起 output_publish 工具调用生成 SVG 流程图，Pillow 按 rich 源码文件数量生成 PNG，另生成并发布 CSV，Kimi 返回真实链接并保存到 SQLite 会话 `4328bbe6ee43`。证据 `.naumi/data/rich-assets-real.json`，截图 `.naumi/data/rich-assets-real.png`。独立构造的实际 Engine.execute_tool 在 moderate 模式中发布成功。

35 项后端定向测试、10 项前端单测、5 项浏览器用例通过；ruff、import、锁文件校验及生产构建通过。浏览器覆盖真实 Kimi Markdown/公式、四标签组件及滑块、实际 SVG/PNG 显示、CSV 下载、无令牌拒绝、刷新恢复。资源浏览器验证通过临时的独立资源 API（18766）转发真实请求，没有替换或重启既有后端。

## 当前边界与上线状态

- 8765 常驻后端仍为旧进程。此前重启动作被自动审批以 `blocked by policy` 拒绝；本次没有重试该动作。正式使用新工具/资源接口需用户重启后端，前端产物已构建。
- 当前真实图片生成包括 Kimi SVG 绘图、代码/Pillow 栅格绘图及已有图片返回；没有接入摄影类文生图供应商，不宣称已支持。
- PDF 等非图片文件提供下载；Markdown、公式、图片、表格、图表、卡片与 HTML 组件原地渲染。
- 发布文件不自动随会话删除，避免共享哈希资源失效；目前没有自动资源回收模块。
- command_index 扩展回归曾发现既有 `/doctor` 参数断言与新增 trace 参数不一致，该断言与本次输出功能无关；未修改。按相关模块的通过结果交付，不宣称全量 pytest 通过。
