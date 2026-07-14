# 终端语义着色设计

## 目标

让 NaumiAgent 的新 Terminal UI 与 Textual TUI 按内容语义稳定着色，而不是把整段输出当作同一种普通文字。用户应当能在不逐字阅读的情况下区分 Git 变化、状态、Markdown 结构、代码和数学表达式，同时保持窄终端、长文本和历史回放的可读性。

本功能是一个独立的渲染改进切片，不改变模型输出、工具执行、消息协议或持久化格式。

## 设计原则

1. **语义优先**：颜色表达内容含义，不以“多彩”为目标。正文保持终端默认色，只有承担导航、状态或语法作用的片段着色。
2. **同义同色**：同一种语义在助手消息、工具结果和完成回执中使用相同颜色。
3. **结构优先于猜测**：代码围栏和 `preview_format` 优先于文本启发式；Git 状态字段优先于对自然语言做正则猜测。
4. **安全降级**：不完整 Markdown、不闭合公式、未知语言、窄终端和超长行必须保留原文，不得丢字或泄漏 ANSI 样式到后续行。
5. **终端适配**：数学表达式保留 LaTeX 源文本并增强视觉层级，不尝试在字符终端伪造二维排版。
6. **中文优先**：新增的用户可见标签和降级提示使用中文。

## 方案选择

### 采用：前端统一语义渲染器

在 `frontend/terminal-ui/src/components/markdown.js` 建立单一渲染入口，把块级识别、行内着色、代码着色和 diff 着色拆为小型纯函数。完成回执继续消费结构化 Git 状态，但复用同一语义调色板。

Textual TUI 已由 Rich Markdown 负责标题、列表、强调和代码块，本轮不复制一套 Markdown 解析器；只补齐完成回执中的结构化 Git 状态标记和数学表达式显示，使两套 UI 的语义一致。

### 不采用：组件内继续堆叠正则

这种方式会让助手正文、工具输出和完成回执产生不同颜色规则，且无法可靠处理代码、链接与公式的嵌套边界。

### 暂不采用：后端发送 Markdown AST

AST 协议适合未来富客户端，但当前会扩大流式事件、历史回放、REST/WebSocket 兼容和版本迁移范围。当前原始 Markdown 已足以在显示端完成可靠的语义着色。

## 语义调色板

| 语义 | ANSI/Rich 样式 | 用途 |
|---|---|---|
| 正文 | 默认前景色 | 普通自然语言 |
| 标题/主要导航 | bold + cyan | Markdown 标题、卡片标题 |
| 次要信息 | dim | 引用、围栏、折叠提示、元数据 |
| 成功/新增 | green | Git 新增、通过、完成 |
| 失败/删除 | red | Git 删除、失败、阻断 |
| 修改/警告 | yellow | Git 修改、未验证、警告 |
| 重命名/链接 | blue 或 cyan | Git 重命名、URL、链接文本 |
| 冲突/高风险 | bold + red | Git 冲突、严重错误 |
| diff hunk/数学 | magenta | `@@` 范围、LaTeX 公式 |
| 行内代码/命令 | yellow | 反引号内容、命令片段 |
| 强调 | bold | Markdown `**strong**` |

颜色必须与文字、符号或标签共同表达含义；不能只靠颜色区分状态。

## Markdown 与普通文本渲染

### 块级结构

- ATX 标题 `#` 至 `######`：标记与文字使用 bold cyan。
- 无序和有序列表：列表标记 cyan，正文继续按行内规则渲染。
- 引用：`>` 标记 blue，引用正文 dim。
- 分隔线：dim。
- 围栏代码：围栏 dim，正文走代码着色器。
- 表格：表头 bold cyan，分隔行 dim；单元格继续走行内规则。
- 普通段落：保持默认前景色，只着色其中明确的行内语义。

### 行内结构

解析优先级固定为：转义字符、行内代码、链接、数学、加粗、斜体、普通文本。已识别片段不会再次被内部规则着色，避免 URL、代码或公式被破坏。

- `` `code` ``：yellow。
- `[label](url)`：标签 blue，URL dim blue；输出仍保留完整可复制文本。
- `$...$` 与 `\(...\)`：magenta。
- `$$...$$` 与 `\[...\]`：作为数学块整行 magenta。
- `**strong**`：bold。
- `*emphasis*` 与 `_emphasis_`：cyan。
- 不闭合标记：原样输出，不吞掉后续文字。

自然语言中的路径、命令和数字不做激进猜测，避免普通正文被“彩虹化”。需要可靠着色时应使用 Markdown 行内代码或结构化字段。

## 代码着色

代码块保持轻量、无依赖着色，至少识别：

- 注释：dim；
- 字符串：green；
- 数字：magenta；
- 关键字：cyan；
- 布尔值、空值：yellow；
- 函数声明名称：blue。

着色器按从左到右的词法片段扫描，字符串和注释优先，不能在已经插入 ANSI 的字符串上继续运行正则。未知语言使用同一安全通用规则；不承诺完整编译器级语法解析。

## Git 与 diff 着色

### Unified diff

- `+` 内容行：green；
- `-` 内容行：red；
- `@@` hunk：magenta；
- `diff --git`：bold cyan；
- `+++`、`---` 文件头：cyan；
- `index`、mode 等元数据：dim；
- 冲突标记 `<<<<<<<`、`=======`、`>>>>>>>`：bold red。

### 完成回执

完成回执不再把 Git 摘要整行变灰。结构化 change status 映射为：

- `added`、`untracked`：green；
- `deleted`、`removed_untracked`：red；
- `modified`：yellow；
- `renamed`、`copied`：cyan；
- `conflicted`：bold red；
- `restored`：blue。

分支名使用 cyan；工作区干净使用 green；工作区有改动使用 yellow；领先使用 green；落后使用 red。Textual TUI 通过 Rich Markdown 标签表达相同语义。

## ANSI 与宽度安全

渲染器必须满足：

- 每个语义片段显式 reset；
- 换行和宽度折行后不会把颜色泄漏到下一条消息；
- `stripAnsi()` 后的文本与输入语义文本一致，仅允许 Markdown 标记因显示目的被保留或规范化；
- `visibleWidth()` 对 CJK、emoji 和 ANSI 片段继续正确；
- 原始模型文本中的终端控制序列不得作为可信样式执行，进入语义解析前只保留可显示文本。

## 数据流

```text
assistant/tool raw text
        │
        ├─ preview_format=diff ──> diff line renderer
        ├─ preview_format=code ──> fenced code renderer
        └─ markdown/text ────────> block classifier
                                      │
                                      ├─ code/diff block
                                      ├─ math block
                                      └─ inline semantic renderer

completion receipt structured changes ──> Git semantic status renderer
```

消息协议和持久化内容保持不变，因此实时消息、历史回放和重试回执自动共享同一显示行为。

## 测试策略

只运行相关小模块测试：

1. `frontend/terminal-ui/test/semantic-rendering.test.js`
   - Markdown 标题、列表、引用、链接、行内代码、强调；
   - 四种数学分隔符与不闭合降级；
   - 代码字符串、数字、关键字、注释不会相互污染；
   - diff 文件头、新增、删除、hunk、冲突；
   - CJK 宽度与 ANSI reset。
2. `frontend/terminal-ui/test/completion-receipt-card.test.js`
   - 每种 Git change status 和 Git 分支状态的颜色语义。
3. `tests/unit/test_tui.py`
   - Textual 完成回执包含可由 Rich Markdown 解析的 Git 状态标记；
   - 数学表达式保持可读且不丢内容。
4. 运行 Node 语法检查、触及文件的 Ruff、`git diff --check`。

不运行全量 Python 或 Node 测试。

## 验收标准

- Git 新增和删除在 diff 与完成回执中分别清晰呈现绿色和红色。
- 普通助手消息能直观看出标题、列表、引用、代码、链接、强调和数学表达式。
- 普通正文仍以默认色为主，不出现无意义的高饱和彩色噪声。
- 新 Terminal UI 与 Textual TUI 对 Git 状态使用相同语义。
- 折行、CJK、未知语法和不闭合标记不丢字、不崩溃、不串色。
- 所有新增行为先有失败测试，再实现至通过。

## 明确限制

- 字符终端不进行二维数学排版，只对 LaTeX 源文本做语义突出。
- 代码着色是面向可读性的轻量词法扫描，不替代完整语言解析器。
- 本轮不修改 Web/Mac 图形界面主题，也不增加第三方 Markdown 或语法高亮依赖。
