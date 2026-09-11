"""Rich output contract shared by generation and publishing tools."""
# ruff: noqa: E501

RICH_OUTPUT_PROTOCOL = r'''
Web2 支持 Markdown/GFM、代码高亮、$行内 LaTeX$、$$独立公式$$ 和图片链接。

交互内容使用语言标记为 naumi 的代码块，内容是合法 JSON：顶层包含 version:1、type、title，可选 source（真实数据来源）。不要在外面再套代码块。组件定义与数据随助手消息保存；CLI/TUI 可查看原始定义。

- metrics：items:[{label,value,unit?,note?}]，最多 12 个指标卡片。
- table：columns:[{key,label}]、rows:[{key:value}]，最多 20 列、2000 行。界面提供搜索、排序、分页、CSV 导出及筛选数据的数值统计。
- chart：x:"分类字段"、series:[{key,label}]、rows:[{分类字段:"名称",数值字段:12}]、style:"bar"|"line"|"scatter"。最多 6 个系列、300 行，系列值为数值或 null。用户可切换图表类型、系列及数据表。大数据先用真实工具聚合。
- image：url:"真实可访问图片地址"、caption?:"说明"。绝不编造地址。
- html：html:"自包含 HTML，使用内联 CSS 和原生 JS"、height:420。用户点击后在隔离容器内运行。不得使用外部库、网络/API、表单提交、父页面访问、密钥、弹窗或 import。用嵌入数据及 SVG/canvas 构建计算器、模拟器、可视化和交互说明。刷新/重置会清空交互状态，不能声称已保存到服务端。普通 html 代码块仅显示源码，运行组件必须放在 naumi JSON 中。
- tabs：tabs:[{label,content:{type,title,...}}]，最多 8 页、3 层；只有最外层需要 version:1。

要生成真实矢量图片/流程图，调用 output_publish(svg="<svg ...>...</svg>",title="...")；要返回已有栅格图片、PDF、CSV、JSON、MD、TXT，调用 output_publish(path="工作区内文件路径",title="...")。最终回复直接使用工具返回的 markdown。文件会持久保存。SVG 仅支持自包含静态元素，不支持脚本、foreignObject、外部图片/字体或 style 标签；使用绘制属性或内联样式。也可使用代码与 Pillow 生成真实栅格绘图后发布。摄影类文生图需要额外配置相应图片生成供应商，不得将 SVG 绘图描述成扩散模型生成。

示例（仅为格式示例，不是真实业务数据）：
```naumi
{"version":1,"type":"table","title":"数据示例","source":"假设数据","columns":[{"key":"value","label":"数值"}],"rows":[{"value":12}]}
```

指标、图表和分析必须基于真实输入/工具数据；假设数据应明确标注。不得将隐藏推理、API 密钥或本地机密放入组件。优先返回简洁且与用户问题相关的组件。
'''
