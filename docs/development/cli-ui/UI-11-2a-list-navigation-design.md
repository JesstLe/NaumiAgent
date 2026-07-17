# UI-11.2a 任务列表精确导航与本地搜索设计

## 用户问题

现有 `/tasks` 面板已经能选择、打开和取消任务，但只能用 `Tab/n/p` 逐项循环，缺少主流
终端用户预期的方向键、分页、首尾定位和关键词搜索。任务刷新后若当前项消失，界面会直接
跳到第一项，也没有解释。

## 本轮边界

本轮只完成 UI-11.2 的独立纵向切片，不提前实现全屏双栏，也不改变 Python Bridge 协议：

1. 聚焦任务面板时，`↑/↓` 精确移动一项；`PageUp/PageDown` 每次有界移动八项；
   `Home/End` 定位首尾。
2. `/tasks search <关键词>` 对已收到的任务视图做大小写不敏感的本地过滤；搜索范围包含
   ID、标题以及 `owner`、`cwd`、artifact 路径等行内详情。
3. `/tasks search clear` 清除搜索。搜索不会请求后端，也不会改变 source/status 权威筛选。
4. 刷新和搜索后先按稳定任务 ID 恢复选择；任务消失时选择原索引最近邻并向用户解释。
5. Renderer 只生成各分区 viewport 内的行；10,000 条输入以 12 次真实渲染 P95 小于
   100ms 作为回归门。

## 状态与渲染

`taskPanel.searchQuery` 是进程内展示状态，不写入 Session 快照，因此重新启动 NaumiAgent
仍是默认干净界面。`taskPanel.items` 只保存当前可导航的匹配项；原始权威文本仍保留在
`tasks` system message 中，清除搜索时可无损重建。

搜索查询加入 render cache key，避免查询变化后复用旧画面。Footer 以无颜色依赖的文本
列出方向键、分页、首尾、详情和搜索入口。

## 验收证据

- 状态测试：分页边界、首尾、详情字段搜索、清除搜索、稳定 ID、消失项最近邻提示。
- 组件测试：非匹配行不渲染；10,000 条事件输出不超过 viewport，P95 小于 100ms。
- 真实终端进程：发送标准 ANSI `Down`、`End`、`Home` 键序列，Footer 依次显示正确任务。

## 后续依赖

全屏列表/详情布局继续复用这套 selection/search 状态；UI-11.1 需要把 owner、dependency、
priority、age 从展示文本提升为正式 view model 字段，UI-11.5 再基于来源能力矩阵增加
retry、open artifact 和 takeover。
