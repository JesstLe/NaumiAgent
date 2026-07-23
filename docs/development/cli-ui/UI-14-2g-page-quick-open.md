# UI-14.2g Page QuickOpen Provider

## 用户结果

New UI 与 Textual TUI 的 `Ctrl+P` QuickOpen 现按
`命令 → 任务 → 会话 → 文件 → Agent → 页面` 循环切换。页面 provider 支持按页面名称、精确命令、
中文/英文关键词和说明搜索当前 terminal surface 实际存在的导航入口。

选择页面只把不带参数的精确导航命令填入 composer，不跳转、不发送模型消息，也不触发页面读取。
用户再次显式提交后，才由既有 slash command 路由打开目标页面或视图。

## 单一权威索引

`src/naumi_agent/ui/page_index.py` 是页面元数据的唯一事实源：

- schema v1 固定公开 `page_id`、`command`、`label`、`description`、`keywords`、`order` 与 `surface`；
- builder 在启动时交叉验证每个页面命令确实存在于对应 surface 的权威 command index；
- `page_id` 与 `command` 均不得重复，顺序由 `order + page_id` 决定；
- 标签、说明和关键词必须 NFKC 规范化、无首尾空白、无控制字符；关键词必须排序且不得重复；
- New UI 包含 `/chat` 对话页；TUI 没有独立 `/chat` route，因此不伪造该页面；
- 两端共同包含任务、Goal、Agent、Workbench、权限、Doctor 和 Evolution 入口。

Ink 与 Textual 不维护本地页面常量。New UI 通过 Bridge 的 `navigation_pages` 消费
`build_terminal_page_index("new_ui")`；TUI 直接消费 `build_terminal_page_index("tui")`。

## 协议与错误模型

- `navigation_pages` 随静态 slash command metadata 一起出现在 ready/完整 status 中；高频状态刷新可同时省略二者；
- New UI 协议入口严格验证字段集合、schema version、边界、排序、唯一性、surface 和控制字符；
- 任意畸形 page entry 会拒绝整个 status 事件，不能降级成可执行的自由文本；
- Python builder 失败时 Bridge fail-closed 公开空数组并记录本地 warning，不建立第二套 fallback 页面表；
- 旧 Bridge 不提供 `navigation_pages` 时 New UI 保持空页面 provider、不猜测页面，并提示升级 Bridge 或切回命令；
- 查询最多 200 grapheme，索引和结果最多 32 项；选择模板只接受单段安全 slash command。

## 搜索与排序

搜索顺序为：

1. 精确 `page_id`、command 或 label；
2. ID/command prefix；
3. label prefix；
4. ID/command/label substring；
5. keywords、description、surface metadata；
6. fuzzy subsequence。

空查询按权威 `order` 展示。当前顺序优先对话、任务、Goal、Agent、Workbench，再到权限、Doctor 和
Evolution，避免按英文 ID 排序造成用户导航跳跃。

## 双端体验

### New UI

- 页面 metadata 经过协议 normalizer 后进入 `state.navigationPages`；
- overlay 显示页面 label、精确 command 和说明，并保留“不会自动发送或执行”文案；
- `Tab` 第五次从 Agent 切换到页面，第六次回到命令；
- 页面选择不会改变 `state.route`；只有用户显式提交 composer 才走既有 route handler。
- 显式提交 `/chat` 现在会返回 conversation route，并关闭 Agent/Inspector 实时订阅，不再只切换输入意图。

### Textual TUI

- `CommandQuickOpenScreen` 直接复用 Python 搜索和模板 helper；
- 页面行与详情展示 label、command、关键词和将填入的精确模板；
- 真实 modal callback 只更新主 composer，不启动 Agent run；
- surface-aware builder 保证 TUI 不显示仅 New UI 支持的 `/chat`。

## 验收证据

- Python 单元验证 surface 差异、command backing、确定性、中文 metadata、fuzzy、边界和非法控制字符；
- Bridge 单元验证完整权威 payload、构建失败 fail-closed，以及精简 status 同时省略静态页面；
- Node 协议验证合法 schema、未知字段、错误 surface、未排序关键词、重复 ID 和缺失字段兼容；
- New UI reducer/render 验证 status → 搜索 → 选择 → composer 全链路，route 保持不变；
- Textual Pilot 真实执行五次 Tab、搜索“目标”并只填入 `/goal`；
- New UI 子进程真实执行五次 Tab、渲染页面 provider、选择 `/goal`，确认未发送 `submit`；
- 仅运行 UI-14.2g 相关 Ruff、compile、Python/Textual、Bridge、Node 与子进程小模块测试，未运行全量测试。

## 未完成边界

- 页面 provider 只负责静态导航，不索引带动态 ID 的 Harness/Evolution 详情页；这些入口继续由对应列表页生成；
- 当前没有用户自定义页面注册机制；后续必须扩展权威 schema，不能让前端直接注入命令；
- 跨启动最近历史、typed argument form、Vim/input mode、完整 composer grapheme 编辑和键位冲突诊断仍未完成，
  因此 UI-14 保持 partial。
