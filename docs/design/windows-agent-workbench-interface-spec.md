# Windows NaumiAgent Workbench Interface Specification

> 本文件是 Windows 端 Workbench 的界面规格，与 macOS 端保持后端合同一致，但在入口布局上采用“聊天为主、左侧导航”的体验。

---

## 1. 设计目标

Windows 端把 **对话（Chat）** 作为默认主界面，原因：

- 对话是最通用的入口，无论用户想提问、创建任务、运行验证，都可以先从自然语言开始。
- 其他治理页面（任务市场、审查、时间线等）是“对话的延伸”，通过左侧导航随时切换。
- 与 Node Terminal UI 的交互心智保持一致：先聊天，再治理。

同时保留 mac app 定义的全部页面和能力，只是调整入口权重。

---

## 2. 全局窗口布局

```text
┌──────────────────────────────────────────────────────────────────────────┐
│  MenuBar：文件 / 编辑 / 视图 / 帮助 / 窗口                                │
├───────────────┬───────────────────────────────────────┬──────────────────┤
│               │                                       │                  │
│  左侧导航栏    │        中央主内容区                    │   右侧面板        │
│               │                                       │                  │
│  总览         │   默认显示 ChatPage                    │   页面上下文     │
│  对话 ★       │                                       │   （Inspector /  │
│  任务市场     │   点击左侧导航后切换为：               │    输出 / 来源   │
│  工作区       │   Dashboard / TaskMarket / Reviews    │    快捷操作）    │
│  审查         │   / Timeline / Worktrees / Settings   │                  │
│  时间线       │                                       │                  │
│  设置         │                                       │                  │
│               │                                       │                  │
├───────────────┤                                       │                  │
│ Workspace     │                                       │                  │
│ Session 列表   │                                       │                  │
│ 全局操作      │                                       │                  │
│ 底部状态      │                                       │                  │
└───────────────┴───────────────────────────────────────┴──────────────────┘
```

### 2.1 顶部 MenuBar

使用 WinUI 3 `MenuBar`，提供全局命令：

- **文件**：新建 Mission、打开 Workspace、退出。
- **编辑**：撤销、重做、复制诊断摘要。
- **视图**：切换语言、显示/隐藏左侧导航栏、显示/隐藏右侧面板、放大/缩小。
- **帮助**：打开日志目录、文档、关于。

### 2.2 左侧导航栏

宽度：**240–280px**，可折叠为仅图标模式（72px）。

内容从上到下：

1. **App 标题区**
   - NaumiAgent Workbench 图标 + 名称
   - 当前 workspace 名称

2. **主导航菜单**

   | 图标 | 标签 | 目标页面 |
   |------|------|----------|
   | 🏠 | 总览 | Dashboard |
   | 💬 | 对话 | Chat（默认高亮） |
   | 📋 | 任务市场 | Task Market |
   | 🌿 | 工作区 | Worktrees |
   | 👁 | 审查 | Reviews |
   | ⏱ | 时间线 | Timeline |
   | ⚙ | 设置 | Settings |

3. **Workspace / Session 列表**
   - 当前激活的 session
   - 最近使用的 session
   - 每个 session 下展示当前 mission 和 open issues 数量

4. **全局操作按钮**
   - 新建 Mission
   - 暂停 Agents
   - 同步上下文
   - 搜索

5. **底部状态**
   - Daemon 连接状态（在线/离线/启动中）
   - 当前 Agent 数量、Open Issues 数量
   - 语言切换按钮

**行为**：

- 点击导航项切换中央 `Frame` 到对应页面。
- 当前页面高亮。
- 窗口宽度变窄时，导航栏可折叠为仅图标，悬停展开完整标签。

### 2.3 中央主内容区

默认显示 **ChatPage**。

通过 `Frame` 导航切换页面：

- `DashboardPage`
- `ChatPage`（默认）
- `TaskMarketPage`
- `ReviewsPage`
- `TimelinePage`
- `WorktreesPage`
- `SettingsPage`

**规则**：

- ChatPage 常驻内存，切换回对话时保留输入框草稿和滚动位置。
- 其他页面可以按需释放，但返回 Chat 时不应重新加载历史消息。
- 页面切换使用 `Frame.Navigate`，带动画（淡入淡出）。

### 2.4 右侧面板

宽度：**320–420px**，可折叠。

作为 **页面级上下文面板**，内容由当前页面决定：

| 当前页面 | 右侧面板内容 |
|----------|--------------|
| Chat | Issue Draft / 输出 / 来源 |
| Dashboard | Inspector（选中对象详情） |
| Task Market | Bid Inspector / Issue 详情 |
| Reviews | Risk Approval Panel |
| Timeline | Failure Detail |
| Worktrees | Worktree Detail |
| Settings | 设置项说明 |

---

## 3. 窗口尺寸

```text
默认窗口：1440 x 1024
最小窗口：1180 x 760
```

### 响应式规则

| 窗口宽度 | 行为 |
|----------|------|
| ≥ 1440px | 左侧导航栏展开（图标+文字）、右侧面板展开。 |
| 1280–1440px | 左侧导航栏保持展开，右侧面板可折叠为窄条。 |
| 1180–1280px | 左侧导航栏折叠为仅图标，悬停展开；右侧面板可隐藏。 |
| < 1180px | 触发最小窗口，不允许继续缩小。 |

---

## 4. Chat 主界面详细规格

### 4.1 默认布局

中央区域默认就是聊天界面，分为三部分：

```text
┌──────────────────────────────────────────────────┐
│ 当前 Session / Mission 标题栏                     │
├──────────────────────────────────────────────────┤
│                                                  │
│  消息列表                                         │
│  （用户消息 + 助手消息）                           │
│                                                  │
├──────────────────────────────────────────────────┤
│  输入框（Composer）                               │
│  [同时创建任务] [发送]                            │
└──────────────────────────────────────────────────┘
```

### 4.2 Session 标题栏

显示：

- 当前 session 名称
- 当前 mission 名称
- 快捷操作：新建 Mission、切换 Mission

### 4.3 消息列表

- 用户消息居右，助手消息居左。
- 支持 Markdown 渲染（代码块、列表、链接）。
- 代码块带复制按钮。
- 若消息创建了 issue，显示关联任务卡片（可点击跳转 Task Market）。

### 4.4 Composer

- 多行文本框，支持 `Ctrl+Enter` 发送。
- 底部工具栏：
  - `同时创建任务` 复选框
  - `附件` 按钮（预留）
  - `发送` 按钮

勾选“同时创建任务”后，展开右侧面板中的 issue draft：

```text
┌──────────────┐
│ Issue Draft  │
│ - mission_id │
│ - title      │
│ - description│
│ - acceptance │
│ - risk_level │
│ - parallel   │
└──────────────┘
```

### 4.5 对话页与治理页的联动

- 创建任务后，右下角弹出提示："已创建任务 #xxx"，并提供跳转 Task Market 按钮。
- 消息中提及 issue、worktree、validation 时，渲染为可点击链接，点击后左侧导航高亮对应页面并跳转。
- 发送"/state"、"/chaos" 等斜杠命令时，结果可在右侧面板展示结构化输出，避免聊天流被长输出淹没。

---

## 5. 左侧导航栏 + 右侧面板组合

左侧导航栏负责 **全局页面切换**，右侧面板负责 **当前页面上下文**。

```text
左侧导航栏                     右侧面板
┌────────────────┐             ┌────────────────┐
│ 总览           │             │ 选中对象详情   │
│ 对话 ★         │             │ 输出/来源      │
│ 任务市场       │             │ 快捷操作       │
│ 工作区         │             │                │
│ 审查           │             │                │
│ 时间线         │             │                │
│ 设置           │             │                │
├────────────────┤             └────────────────┘
│ Workspace      │
│ Session 列表   │
│ 全局操作       │
│ 底部状态       │
└────────────────┘
```

---

## 6. 导航状态管理

- `AppState.CurrentRoute` 记录当前页面，默认 `Chat`。
- 左侧导航栏绑定 `AppState.CurrentRoute`。
- 启动时若配置中保存了上次页面，恢复上次页面；否则进入 Chat。
- 窗口关闭时保存当前 route 到 `settings.json`。

---

## 7. 与 macOS 端的差异

| 项 | macOS 端 | Windows 端 |
|----|----------|------------|
| 默认首页 | Dashboard | Chat |
| 主导航位置 | 顶部工具栏 | 左侧导航栏 |
| 左侧边栏 | Mission Tree + Issue Queue | 页面导航 + Workspace/Session 列表 |
| 右侧面板 | Inspector | 页面上下文 / 输出 / Inspector |
| 交互入口 | 治理优先 | 对话优先，治理为延伸 |

**理由**：Windows 端优先对齐 Terminal UI 和日常对话心智；后端合同完全一致，UI 布局属于客户端本地决策。

---

## 8. 中文文案

### 8.1 左侧导航

```text
nav.dashboard = "总览"
nav.chat = "对话"
nav.taskMarket = "任务市场"
nav.worktrees = "工作区"
nav.reviews = "审查"
nav.timeline = "时间线"
nav.settings = "设置"
```

### 8.2 全局操作

```text
action.newMission = "新建 Mission"
action.pauseAgents = "暂停 Agent"
action.syncContext = "同步上下文"
action.search = "搜索"
```

### 8.3 Chat 页面

```text
chat.composerPlaceholder = "输入问题或指令..."
chat.createLinkedIssue = "同时创建任务"
chat.send = "发送"
chat.issueCreatedToast = "已创建任务 {issue_id}"
chat.goToTaskMarket = "查看任务市场"
```

---

## 9. 可访问性

- 左侧导航栏每个按钮必须有可访问名称。
- 当前页面高亮不能仅依赖颜色，需同时有图标/文字状态变化。
- 聊天消息列表支持键盘导航。
- 左右边栏支持 `F6` 或 `Ctrl+Shift+L/R` 快速聚焦。

---

## 10. MVP 切分

### 必须实现

- 三栏 Shell（MenuBar + 左侧导航栏 + 中央 Frame + 右侧面板）。
- Chat 为默认页面，消息列表、Composer、创建关联任务。
- 左侧导航切换到 Dashboard / Task Market / Reviews / Timeline / Worktrees / Settings。
- 语言切换（zh-CN / en-US）。
- 窗口状态保存与恢复。

### 可后续增强

- 左侧导航栏折叠/展开动画。
- 右侧面板完整 Inspector。
- 斜杠命令结构化输出面板。
- 多 workspace 切换。

---

## 11. WinUI 3 控件映射

| UI 元素 | WinUI 3 控件 |
|---------|--------------|
| 顶部菜单 | `MenuBar` |
| 左侧导航栏 | `Grid` + `ItemsRepeater` + `Button` |
| 中央页面切换 | `Frame` |
| 右侧面板 | `Grid` + `ContentControl` 绑定页面级 ViewModel |
| 消息列表 | `ListView` 自定义 `DataTemplate` |
| Composer | `TextBox`（多行）+ `Button` |
| 状态条 | `InfoBar` / 自定义 `Grid` |

---

## 12. 关键文件

- `apps/windows/NaumiAgentWorkbench/NaumiAgentWorkbench/Views/Shell/MainWindow.xaml`
- `apps/windows/NaumiAgentWorkbench/NaumiAgentWorkbench/Views/Shell/LeftNavigationRail.xaml`
- `apps/windows/NaumiAgentWorkbench/NaumiAgentWorkbench/Views/Shell/RightContextPanel.xaml`
- `apps/windows/NaumiAgentWorkbench/NaumiAgentWorkbench/Views/ChatPage.xaml`
- `apps/windows/NaumiAgentWorkbench/NaumiAgentWorkbench/ViewModels/MainWindowViewModel.cs`
- `apps/windows/NaumiAgentWorkbench/NaumiAgentWorkbench/Core/AppRoute.cs`
