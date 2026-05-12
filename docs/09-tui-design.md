# NaumiAgent TUI 设计方案

> 基于 Textual（Python TUI 框架）设计，遵循 Agent UX 五大原则：透明性、可控性、状态可见性、错误恢复、渐进授权。

## 1. 设计原则

基于 Fuselab Creative 的 Agent UX 研究和 Anthropic 的 Agent 设计指南：

| 原则 | 含义 | TUI 中的实现 |
|------|------|-------------|
| **透明性** | 展示 Agent 每一步的推理和决策 | 工具调用实时流式展示、计划步骤可视化 |
| **可控性** | 用户可在任意阶段覆盖、暂停、重定向 | 执行中可编辑/跳过步骤、危险操作确认 |
| **状态可见** | 主动告知 Agent 正在做什么 | 状态栏实时显示当前动作、进度百分比 |
| **错误恢复** | 解释失败原因并建议下一步 | 错误面板显示 "发生了什么 → 为什么 → 怎么办" |
| **渐进授权** | 从受限自治逐步扩展 | 权限模式可切换，首次操作需确认 |

### 核心设计决策：对话和活动分离

```
传统聊天界面：
  [用户消息] → [Agent 回复] → [用户消息] → [Agent 回复]
  ❌ 工具调用和推理混在对话流里，难以阅读

NaumiAgent TUI：
  ┌─ 对话面板 ─────┐  ┌─ 活动面板 ──────┐
  │ 用户：帮我重构  │  │ 📋 计划：3步     │
  │ Agent：好的，我 │  │ ✓ 步骤1 读取文件 │
  │ 来帮你处理。    │  │ ● 步骤2 分析结构 │
  │                 │  │ ○ 步骤3 执行重构 │
  │ Agent：重构完成  │  │ 🔧 file_edit     │
  │                 │  │   main.py:12-18  │
  └─────────────────┘  └──────────────────┘
  ✅ 对话和活动分开，信息结构清晰
```

## 2. 布局设计

### 2.1 主界面 ASCII 布局

```
┌─────────────────────────────────────────────────────────────────────────┐
│ NaumiAgent v0.1 │ 会话: default │ 模式: moderate │ 🟢 Ready        [×] │
├─────────────────────────────────┬───────────────────────────────────────┤
│                                 │ 📋 执行计划                          │
│  🧑 你                          │ ──────────────────────────────────── │
│  帮我重构 main.py 中的数据处理   │ ✓ 1. 读取 main.py                    │
│  逻辑，把大函数拆小              │ ● 2. 分析函数依赖关系                 │
│                                 │   └─ 正在分析 3 个函数...              │
│  🤖 NaumiAgent                  │ ○ 3. 拆分数据处理函数                 │
│  好的，我来帮你重构。先看一下    │ ○ 4. 编写单元测试                    │
│  文件结构。                      │ ○ 5. 运行测试验证                    │
│                                 │                                       │
│  已完成分析，发现 3 个需要拆分    │ 🔧 工具调用                           │
│  的大函数：                       │ ──────────────────────────────────── │
│  • process_data() — 85 行       │ 10:32:05 file_read main.py ✓         │
│  • transform_records() — 62 行  │ 10:32:08 file_search "def " ✓        │
│  • validate_input() — 45 行     │ 10:32:12 code_execute analyze.py ●   │
│                                 │                                       │
│  我建议分 5 步完成，已在右侧列    │ 📊 资源                               │
│  出计划。要我开始执行吗？         │ ──────────────────────────────────── │
│                                 │ Token: 12,340 / 500K                 │
│  > 输入消息...            [发送]  │ 费用: $0.08 / $5.00                  │
│                                 │ 轮次: 5/30                           │
├─────────────────────────────────┴───────────────────────────────────────┤
│ 🤖 思考中... | 💰 $0.08 | 📝 12.3K tokens | ⏱ 32s | [F1帮助] [Ctrl+C中断] │
└─────────────────────────────────────────────────────────────────────────┘
```

### 2.2 布局区域说明

```
┌─────────────────────────────────────────────────────────┐
│  Header — 标题栏（固定）                                 │
│  显示：Agent 名称、会话 ID、权限模式、连接状态            │
├────────────────────────┬────────────────────────────────┤
│                        │                                │
│  Chat Panel            │  Activity Panel                │
│  对话面板（70% 宽度）    │  活动面板（30% 宽度）           │
│                        │                                │
│  内容：                │  内容：                         │
│  - 用户消息            │  - 执行计划（可折叠）            │
│  - Agent 回复          │  - 工具调用日志（滚动）          │
│  - Markdown 渲染       │  - 资源监控                     │
│                        │                                │
│                        │  可通过 Tab 切换子面板           │
├────────────────────────┴────────────────────────────────┤
│  Input Bar — 输入栏（固定）                               │
│  内容：多行输入框 + 发送按钮 + 快捷操作                    │
├─────────────────────────────────────────────────────────┤
│  Footer — 状态栏（固定）                                  │
│  显示：当前状态、费用、Token、耗时、快捷键提示              │
└─────────────────────────────────────────────────────────┘
```

## 3. 各面板详细设计

### 3.1 对话面板（Chat Panel）

**功能**：用户和 Agent 的主要交互区域。

**消息类型**：

```
┌──────────────────────────────────────────────┐
│ 🧑 你                                    10:32│
│ 帮我重构 main.py 中的数据处理逻辑            │
├──────────────────────────────────────────────┤
│ 🤖 NaumiAgent                             10:32│
│                                              │
│ 好的，我来分析一下文件结构。                  │
│                                              │
│ 发现 3 个需要拆分的函数：                     │
│                                              │
│ 1. `process_data()` — 85 行，混合了读取、    │
│    清洗、转换三个职责                         │
│ 2. `transform_records()` — 62 行，包含      │
│    3 种不同的转换逻辑                         │
│ 3. `validate_input()` — 45 行，验证规则     │
│    和错误处理耦合在一起                        │
│                                              │
│ 建议的拆分方案已在右侧计划面板展示。           │
│ 确认后我开始执行。                            │
├──────────────────────────────────────────────┤
│ ⚠️ 确认操作                              10:33│
│                                              │
│ Agent 请求执行以下操作：                      │
│ • file_edit main.py (修改 3 处)              │
│ • file_write test_data_processing.py (新建)  │
│                                              │
│ [✓ 确认执行]  [✗ 拒绝]  [修改计划]            │
└──────────────────────────────────────────────┘
```

**Markdown 渲染支持**：

```python
# 对话面板支持以下 Markdown 元素的终端渲染：
# - 代码块（语法高亮）
# - 列表
# - 粗体/斜体
# - 表格
# - 标题
# - 行内代码
```

### 3.2 活动面板（Activity Panel）

活动面板有 3 个 Tab 页，用户可通过 Tab 键切换。

#### Tab 1：执行计划（Plan）

```
┌─────────────────────────────────┐
│ 📋 执行计划              [编辑] │
├─────────────────────────────────┤
│                                 │
│ ✓ 1. 读取 main.py              │
│     └─ 完成 (0.3s)             │
│                                 │
│ ● 2. 分析函数依赖关系           │  ← 当前步骤（脉冲动画）
│     └─ 正在执行...             │
│                                 │
│ ○ 3. 拆分数据处理函数           │  ← 待执行
│                                 │
│ ○ 4. 编写单元测试               │
│                                 │
│ ○ 5. 运行测试验证               │
│                                 │
├─────────────────────────────────┤
│ 进度: ████████░░░░░ 40% (2/5)  │
└─────────────────────────────────┘

快捷键：
  [e] 编辑当前步骤
  [s] 跳过当前步骤
  [p] 暂停执行
  [r] 重新规划
```

**计划步骤状态**：
- `○` 待执行（灰色）
- `●` 执行中（绿色脉冲动画）
- `✓` 已完成（绿色）
- `✗` 失败（红色）
- `⊘` 已跳过（灰色删除线）

#### Tab 2：工具调用（Tools）

```
┌─────────────────────────────────┐
│ 🔧 工具调用                     │
├─────────────────────────────────┤
│                                 │
│ 10:32:05 file_read ✓ (0.1s)    │
│   📄 main.py (2.1KB)           │
│                                 │
│ 10:32:08 file_search ✓ (0.3s)  │
│   🔍 pattern="def " in main.py │
│   → 找到 7 个匹配              │
│                                 │
│ 10:32:12 code_execute ● (2.1s) │
│   💻 analyze.py                 │
│   └─ 运行中...                  │
│                                 │
│ 10:32:15 file_edit ○            │
│   ✏️ main.py:45-62              │
│                                 │
│ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ │
│                                 │
│ 统计:                           │
│   调用: 7 次                    │
│   成功: 5 次                    │
│   失败: 1 次                    │
│   总耗时: 4.2s                  │
│                                 │
└─────────────────────────────────┘

快捷键：
  [Enter] 展开详情
  [f] 过滤工具类型
```

#### Tab 3：资源监控（Resources）

```
┌─────────────────────────────────┐
│ 📊 资源监控                     │
├─────────────────────────────────┤
│                                 │
│ Token 使用                      │
│ ████████░░░░░░ 12,340 / 500K   │
│                                 │
│ 费用                            │
│ ██░░░░░░░░░░░░ $0.08 / $5.00   │
│                                 │
│ 模型分布                        │
│   Sonnet 4.6  ████████  85%    │
│   Haiku 4.5   ██         15%   │
│                                 │
│ 执行轮次                        │
│   5 / 30 (16%)                  │
│                                 │
│ 工具调用                        │
│   7 次 | 成功率 85%             │
│                                 │
│ 会话时长                        │
│   ⏱ 2m 34s                     │
│                                 │
│ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ │
│ 历史会话                        │
│   5/10 修复登录bug   $0.12      │
│   5/09 写单元测试    $0.05      │
│   5/08 重构API       $0.18      │
│                                 │
└─────────────────────────────────┘
```

### 3.3 输入栏（Input Bar）

```
┌──────────────────────────────────────────────────────────┐
│ > 帮我把 transform_records 也拆一下                       │
│                                                          │
│                                                          │
├──────────────────────────────────────────────────────────┤
│ [Enter 发送] [Shift+Enter 换行] [/ 命令] [Tab 切换面板]  │
└──────────────────────────────────────────────────────────┘
```

**斜杠命令**：

```
/plan <task>      — 只生成计划，不执行
/run <task>       — 直接执行任务（跳过确认）
/edit             — 编辑当前计划
/skip             — 跳过当前步骤
/pause            — 暂停执行
/resume           — 恢复执行
/replan           — 重新规划
/model <name>     — 切换模型
/mode <mode>      — 切换权限模式
/session <id>     — 切换/恢复会话
/save             — 保存当前会话
/clear            — 清空对话
/undo             — 撤销上一步操作
/history          — 查看会话历史
/tools            — 列出可用工具
/budget           — 查看预算详情
/help             — 显示帮助
/quit             — 退出
```

### 3.4 状态栏（Footer）

```
├─────────────────────────────────────────────────────────────────────┤
│ 🤖 思考中... | 💰 $0.08 | 📝 12.3K tok | ⏱ 32s | F1帮助 Ctrl+C中断 │
└─────────────────────────────────────────────────────────────────────┘
```

**状态指示器**：

| 状态 | 显示 | 含义 |
|------|------|------|
| Ready | 🟢 Ready | 空闲，等待输入 |
| Thinking | 🤖 思考中... | LLM 推理中 |
| Tool Call | 🔧 调用 file_read | 正在执行工具 |
| Waiting | ⏳ 等待确认 | 等待用户确认操作 |
| Error | 🔴 出错 | 执行出错 |
| Paused | ⏸ 已暂停 | 用户暂停了执行 |

## 4. 特殊界面状态

### 4.1 启动界面

```
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│                                                             │
│                                                             │
│              _   _                  _                       │
│             | \ | | __ _ _ __   ___| |_ _   _               │
│             |  \| |/ _` | '_ \ / _ \ __| | | |              │
│             | |\  | (_| | | | |  __/ |_| |_| |              │
│             |_| \_|\__,_|_| |_|\___|\__|\__, |              │
│              Agent                      |___/               │
│                                          v0.1.0             │
│                                                             │
│         通用智能 Agent — 文件操作 · 代码 · 浏览器 · 搜索     │
│                                                             │
│                                                             │
│   模型: Claude Sonnet 4.6    权限: moderate    会话: new    │
│                                                             │
│   输入任务开始对话，输入 /help 查看帮助                       │
│                                                             │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 4.2 确认对话框（危险操作）

```
┌─ ⚠️ 确认操作 ──────────────────────────────────────────────┐
│                                                            │
│  Agent 请求执行以下危险操作：                                │
│                                                            │
│  🔧 bash_run                                               │
│  命令：pip install -r requirements.txt                      │
│                                                            │
│  影响：                                                    │
│  • 将安装 12 个依赖包                                      │
│  • 修改当前 Python 环境                                    │
│                                                            │
│  ┌──────────────┐  ┌──────────┐  ┌────────────┐            │
│  │ ✓ 允许执行   │  │ ✗ 拒绝   │  │ 一直允许    │            │
│  └──────────────┘  └──────────┘  └────────────┘            │
│                                                            │
│  [Enter 确认] [Esc 取消] [Tab 切换]                         │
└────────────────────────────────────────────────────────────┘
```

### 4.3 错误恢复界面

```
┌─ ❌ 步骤执行失败 ──────────────────────────────────────────┐
│                                                            │
│  步骤 3: 拆分数据处理函数                                   │
│                                                            │
│  ┌─ 发生了什么 ─────────────────────────────────────┐      │
│  │ file_edit 执行失败：无法匹配 old_text              │      │
│  │ "def process_data(data: list) -> dict:"           │      │
│  │ 文件内容可能在之前的步骤中被修改                     │      │
│  └──────────────────────────────────────────────────┘      │
│                                                            │
│  ┌─ 为什么 ─────────────────────────────────────────┐      │
│  │ Agent 的 old_text 基于步骤 1 读取的文件内容        │      │
│  │ 但步骤 2 的 code_execute 可能已修改了 main.py     │      │
│  └──────────────────────────────────────────────────┘      │
│                                                            │
│  ┌─ 怎么办 ─────────────────────────────────────────┐      │
│  │ 1. 重新读取文件，基于最新内容重试                   │      │
│  │ 2. 跳过此步骤，手动处理                            │      │
│  │ 3. 重新规划整个任务                                │      │
│  └──────────────────────────────────────────────────┘      │
│                                                            │
│  [1 重试] [2 跳过] [3 重新规划] [Esc 取消任务]              │
└────────────────────────────────────────────────────────────┘
```

### 4.4 浏览器预览模式

当 Agent 执行浏览器操作时，活动面板切换为浏览器预览：

```
┌─────────────────────────────────┐
│ 🌐 浏览器 — google.com          │
├─────────────────────────────────┤
│ ┌─────────────────────────────┐ │
│ │  ┌───┬───┬───┐             │ │
│ │  │ O │ O │ O │ google.com  │ │    ← 页面缩略图
│ │  └───┴───┴───┘             │ │       (Sixel/Kitty
│ │                             │ │        图片协议)
│ │  [Google 搜索框]            │ │
│ │                             │ │
│ │  Google 搜索  手气不错      │ │
│ │                             │ │
│ └─────────────────────────────┘ │
│                                 │
│ 操作：                          │
│ ● navigate → google.com ✓       │
│ ○ type "#search" → "Python TUI" │
│                                 │
│ [s 截图] [t 文本模式] [Esc 关闭]│
└─────────────────────────────────┘
```

## 5. 快捷键

### 5.1 全局快捷键

| 快捷键 | 功能 |
|--------|------|
| `Enter` | 发送消息 / 确认 |
| `Shift+Enter` | 输入换行 |
| `Ctrl+C` | 中断当前执行 |
| `Ctrl+D` | 退出 NaumiAgent |
| `Tab` | 切换焦点面板 |
| `Shift+Tab` | 反向切换面板 |
| `F1` | 显示帮助 |
| `F2` | 聚焦对话面板 |
| `F3` | 聚焦活动面板 |
| `F4` | 切换活动面板 Tab |
| `Ctrl+L` | 清空对话 |
| `Ctrl+S` | 保存会话 |
| `Ctrl+Z` | 撤销上一步 |
| `↑` / `↓` | 浏览历史消息 |
| `Ctrl+↑` / `Ctrl+↓` | 滚动对话面板 |

### 5.2 活动面板快捷键

| 快捷键 | 功能 |
|--------|------|
| `e` | 编辑当前步骤 |
| `s` | 跳过当前步骤 |
| `p` | 暂停/恢复执行 |
| `r` | 重新规划 |
| `Enter` | 展开工具调用详情 |
| `f` | 过滤工具类型 |

## 6. 主题与配色

### 6.1 默认暗色主题

```css
/* NaumiAgent TCSS 主题 */

Screen {
    background: $surface;
    color: $text;
}

/* 对话面板 */
.chat-panel {
    background: $surface;
    border-right: solid $primary;
}

/* 用户消息 */
.user-message {
    color: $text;
    background: $primary-darken-3;
    padding: 1 2;
    margin: 1 0;
}

/* Agent 消息 */
.agent-message {
    color: $text;
    background: $surface-darken-1;
    padding: 1 2;
    margin: 1 0;
}

/* 代码块 */
.code-block {
    background: $surface-darken-3;
    color: $success;
    padding: 1 2;
}

/* 活动面板 */
.activity-panel {
    background: $surface-darken-1;
}

/* 计划步骤 */
.step-completed { color: $success; }
.step-active { color: $warning; text-style: bold; }
.step-pending { color: $text-disabled; }
.step-failed { color: $error; }
.step-skipped { color: $text-disabled; text-style: italic; }

/* 进度条 */
.progress-bar {
    color: $success;
}

/* 状态栏 */
.footer {
    background: $primary;
    color: $text;
}
```

### 6.2 配色方案

```
暗色主题（默认）：
  背景:   #1e1e2e (Catppuccin Mocha Base)
  文字:   #cdd6f4 (Catppuccin Mocha Text)
  主色:   #89b4fa (Blue)
  成功:   #a6e3a1 (Green)
  警告:   #f9e2af (Yellow)
  错误:   #f38ba8 (Red)
  强调:   #cba6f7 (Mauve)
  边框:   #45475a (Surface0)

亮色主题：
  背景:   #eff1f5
  文字:   #4c4f69
  主色:   #1e66f5
  成功:   #40a02b
  警告:   #df8e1d
  错误:   #d20f39
```

## 7. 响应式布局

终端宽度变化时自适应：

```
宽终端 (≥120 列)：
┌──────────────────────────┬──────────────────────────┐
│                          │                          │
│     Chat Panel (70%)     │   Activity Panel (30%)   │
│                          │                          │
└──────────────────────────┴──────────────────────────┘

中等终端 (80-119 列)：
┌─────────────────────────────────────────────────────┐
│                                                     │
│     Chat Panel (100%)  [F4 切换到 Activity Panel]   │
│                                                     │
└─────────────────────────────────────────────────────┘

窄终端 (<80 列)：
┌────────────────────────────┐
│ Chat (简化模式)             │
│ • 不显示 Markdown 渲染      │
│ • 工具调用内联显示          │
│ • 状态栏精简               │
└────────────────────────────┘
```

## 8. 动画与交互反馈

| 场景 | 动画 | 持续时间 |
|------|------|---------|
| Agent 思考中 | 思考气泡脉冲动画 | 持续 |
| 工具执行中 | 步骤指示器旋转 | 持续 |
| 步骤完成 | ✓ 淡入 + 短暂高亮 | 0.5s |
| 步骤失败 | ✗ 闪烁红色 | 1s |
| 进度更新 | 进度条平滑增长 | 0.3s |
| 新消息到达 | 消息滑入 | 0.2s |
| 确认弹窗 | 从底部滑入 | 0.2s |
| 错误恢复 | 从右侧滑入 | 0.2s |

## 9. 技术实现方案

### 9.1 框架选择：Textual

**选择理由**：
- Python 原生 TUI 框架，和 NaumiAgent 技术栈一致
- 基于Rich 构建，内置 Markdown 渲染
- CSS-like 样式系统（TCSS）
- 事件驱动架构，适合异步 Agent 交互
- 支持鼠标交互、键盘导航、动画

**依赖**：

```toml
dependencies = [
    "textual>=3.0",      # TUI 框架
    "rich>=14.0",        # 终端渲染（Textual 依赖）
]
```

### 9.2 目录结构

```
src/naumi_agent/
├── interface/
│   ├── __init__.py
│   ├── app.py              # Textual App 主类
│   ├── screens/            # 屏幕定义
│   │   ├── __init__.py
│   │   ├── chat.py         # 主聊天屏幕
│   │   ├── confirm.py      # 确认对话框
│   │   ├── error.py        # 错误恢复界面
│   │   ├── help.py         # 帮助屏幕
│   │   └── startup.py      # 启动屏幕
│   ├── widgets/            # 自定义组件
│   │   ├── __init__.py
│   │   ├── chat_panel.py   # 对话面板
│   │   ├── activity_panel.py # 活动面板
│   │   ├── plan_view.py    # 计划视图
│   │   ├── tool_log.py     # 工具调用日志
│   │   ├── resource_monitor.py # 资源监控
│   │   ├── input_bar.py    # 输入栏
│   │   ├── status_bar.py   # 状态栏
│   │   └── message.py      # 消息气泡组件
│   └── styles/             # TCSS 样式
│       ├── base.tcss       # 基础样式
│       ├── dark.tcss       # 暗色主题
│       └── light.tcss      # 亮色主题
```

### 9.3 核心 App 类

```python
# src/naumi_agent/interface/app.py

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header

class NaumiApp(App):
    """NaumiAgent TUI 主应用"""

    CSS_PATH = "styles/base.tcss"

    BINDINGS = [
        Binding("ctrl+c", "interrupt", "中断执行", show=True),
        Binding("ctrl+d", "quit", "退出", show=False),
        Binding("f1", "show_help", "帮助", show=True),
        Binding("f2", "focus_chat", "对话", show=False),
        Binding("f3", "focus_activity", "活动", show=False),
        Binding("f4", "cycle_activity_tab", "切换Tab", show=False),
        Binding("ctrl+l", "clear_chat", "清空对话", show=False),
        Binding("ctrl+s", "save_session", "保存", show=False),
    ]

    def __init__(self, engine_config=None):
        super().__init__()
        self.engine = None  # AgentEngine 注入
        self.engine_config = engine_config

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            yield ChatPanel(id="chat-panel")
            yield ActivityPanel(id="activity-panel")
        yield InputBar(id="input-bar")
        yield StatusBar()

    async def on_mount(self) -> None:
        """初始化 Agent 引擎"""
        from ..orchestrator.engine import AgentEngine
        self.engine = AgentEngine(self.engine_config)
        self.query_one("#input-bar").focus()

    async def action_interrupt(self) -> None:
        """中断当前执行"""
        if self.engine:
            await self.engine.interrupt()
            self.notify("执行已中断")

    async def action_show_help(self) -> None:
        self.push_screen(HelpScreen())

    async def handle_user_message(self, message: str) -> None:
        """处理用户输入"""
        chat = self.query_one("#chat-panel", ChatPanel)
        chat.add_user_message(message)

        status = self.query_one(StatusBar)
        status.set_thinking()

        try:
            result = await self.engine.run(message)

            chat.add_agent_message(result.response)

            status.set_ready(
                cost=result.usage.total_cost,
                tokens=result.usage.total_tokens,
                turns=result.turns,
            )
        except Exception as e:
            chat.add_error_message(str(e))
            status.set_error()

    async def handle_tool_call(self, tool_name: str, args: dict) -> None:
        """实时更新工具调用状态（由 Agent 引擎回调）"""
        activity = self.query_one("#activity-panel", ActivityPanel)
        activity.add_tool_call(tool_name, args)

    async def handle_plan_update(self, plan) -> None:
        """实时更新计划状态"""
        activity = self.query_one("#activity-panel", ActivityPanel)
        activity.update_plan(plan)

    async def handle_step_complete(self, step_id: str, result) -> None:
        """步骤完成回调"""
        activity = self.query_one("#activity-panel", ActivityPanel)
        activity.mark_step_completed(step_id, result)

    async def request_confirmation(self, operation: str) -> bool:
        """弹出确认对话框"""
        from .screens.confirm import ConfirmScreen
        result = await self.push_screen_wait(ConfirmScreen(operation))
        return result
```

### 9.4 关键组件示例

```python
# src/naumi_agent/interface/widgets/chat_panel.py

from textual.widgets import Static, RichLog
from textual.containers import VerticalScroll
from rich.markdown import Markdown
from rich.text import Text

class ChatPanel(VerticalScroll):
    """对话面板 — 显示用户和 Agent 的消息"""

    DEFAULT_CSS = """
    ChatPanel {
        width: 70%;
        height: 1fr;
        padding: 1 2;
        border-right: solid $primary;
    }
    """

    def add_user_message(self, content: str) -> None:
        msg = UserMessage(content)
        self.mount(msg)
        self.scroll_end(animate=False)

    def add_agent_message(self, content: str) -> None:
        msg = AgentMessage(content)
        self.mount(msg)
        self.scroll_end(animate=False)

    def add_error_message(self, error: str) -> None:
        msg = ErrorMessage(error)
        self.mount(msg)
        self.scroll_end(animate=False)


class UserMessage(Static):
    """用户消息气泡"""

    DEFAULT_CSS = """
    UserMessage {
        background: $primary-darken-3;
        padding: 1 2;
        margin: 1 0;
        border-left: solid $primary;
    }
    """

    def __init__(self, content: str):
        super().__init__(Text(f"🧑 {content}"))


class AgentMessage(Static):
    """Agent 消息气泡（支持 Markdown 渲染）"""

    DEFAULT_CSS = """
    AgentMessage {
        background: $surface-darken-1;
        padding: 1 2;
        margin: 1 0;
    }
    """

    def __init__(self, content: str):
        super().__init__(Markdown(content))
```

```python
# src/naumi_agent/interface/widgets/plan_view.py

from textual.widgets import Static, Button
from textual.containers import Vertical

class PlanView(Vertical):
    """执行计划视图"""

    DEFAULT_CSS = """
    PlanView {
        height: auto;
        padding: 1;
    }
    .step { padding: 0 1; height: 3; }
    .step-completed { color: $success; }
    .step-active { color: $warning; text-style: bold; }
    .step-pending { color: $text-disabled; }
    .step-failed { color: $error; }
    """

    def update_plan(self, plan) -> None:
        self.query(".step").remove()

        icons = {
            "completed": "✓",
            "active": "●",
            "pending": "○",
            "failed": "✗",
            "skipped": "⊘",
        }

        for step in plan.steps:
            icon = icons.get(step.status, "○")
            css_class = f"step-{step.status}"
            detail = f" └─ {step.detail}" if step.detail else ""

            step_widget = Static(
                f" {icon} {step.id}. {step.description}\n{detail}",
                classes=f"step {css_class}",
            )
            self.mount(step_widget)

    def mark_completed(self, step_id: str, result=None) -> None:
        # 更新步骤状态
        pass

    def mark_failed(self, step_id: str, error: str) -> None:
        pass
```

### 9.5 入口集成

```python
# src/naumi_agent/main.py — 添加 TUI 入口

@app.command()
def tui(
    config: str = typer.Option("config.yaml", "--config", "-c"),
):
    """启动终端 UI"""
    from .config.settings import AppConfig
    from .interface.app import NaumiApp

    app_config = AppConfig.from_yaml(config)
    app = NaumiApp(engine_config=app_config)
    app.run()

# 启动命令：
# naumi tui           # 启动 TUI
# naumi chat          # 启动简单交互式对话
# naumi run "task"    # 执行单个任务
```
