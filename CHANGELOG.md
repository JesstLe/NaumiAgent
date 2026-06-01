# Changelog

All notable changes to this project will be documented in this file.

## [0.1.7] - 2026-06-01

### Added
- **MCTS 工具确定性落地** — `/mcts` 在模型路由未初始化时也会返回 Path A/B/C 多路径探索、KEEP/PRUNE 决策、Winning Path、验证方案和回退触发条件。

### Fixed
- `_scan_mcts` 现在能处理没有 import 的源码文件，不再因外部依赖集合未初始化而失败。

## [0.1.6] - 2026-06-01

### Added
- **JIT 工具确定性落地** — `/jit` 在模型路由未初始化时也会生成可运行 Python 脚本，并返回执行状态。
- **安全算术执行** — `/jit` 可识别简单算术表达式，通过受限 AST 求值返回确定性结果；非直接算术任务会生成可运行的任务分类/验证脚手架。

## [0.1.5] - 2026-06-01

### Added
- **DSPy 工具确定性落地** — `/dspy` 在模型路由未初始化时也会返回 Prompt 模板、Few-shot、Metric、可配置性和成熟度评分扫描结果。
- **Baseline Metric 生成** — `/dspy` 现在会生成可执行的 `score_output()` 启发式评价函数，作为后续 DSPy 编译优化的最小可运行起点。

## [0.1.4] - 2026-06-01

### Changed
- **GraphRAG 工具确定性落地** — `/graph` 在模型路由未初始化时也会返回实体节点、关系边、循环依赖、连通分量和度中心性扫描结果；模型可用时再追加 LLM 图谱推演。
- **GraphRAG 方法归属修复** — AST 图扫描现在会把类方法标记为 `module:Class.method`，避免方法节点丢失所属类上下文。

## [0.1.3] - 2026-06-01

### Added
- **工具发现能力** — 新增 `tool_search`，基于真实 `ToolRegistry` 搜索工具名称、描述、参数和能力标签，支持精确选择与必选关键词。
- **分析工具确定性落地** — `/vibe` 可生成并可选写入 Python/Node/static 可运行 Demo scaffold；`/entropy` 可在无模型时生成 3 句熵减锚点；`/eval` 可生成可运行的 baseline pytest；`/heal` 可从 traceback 生成确定性自愈诊断。
- **无模型静态分析 fallback** — `/chaos`、`/scale`、`/state` 现在即使模型路由未初始化，也会返回真实静态扫描证据。

### Changed
- **权限决策结构化** — 权限拒绝原因增加结构化 code/risk level，用户可见错误提示改为中文。
- **analysis 模块拆分起步** — 抽出 `analysis_common.py` 承载目标解析、源码读取、LLM 调用和 router fallback，降低后续按能力族拆分风险。

### Fixed
- scheduler 通知注入不再隐式启动后台 runner，避免异步副作用。

## [0.1.2] - 2025-05-17

### Added
- **浏览器调试系统全量移植** — 从 browser-debugging-daemon 移植 8 个阶段：
  - Phase 1: SoM (Set-of-Mark) 交互元素标注系统
  - Phase 2: BrowserRuntime — CDP 连接、截图、网络录制、下载管理
  - Phase 3: 25 个 SoM 浏览器工具（goto/observe/click/type/hover/scroll 等）
  - Phase 4: 浏览器子 Agent — LLM 规划、CAPTCHA 处理、自动任务执行
  - Phase 5: TaskRunner — 队列化任务运行器、状态机、模板、断点恢复
  - Phase 6: 25 模块安全扫描器 + 多 Agent 并行扫描协调器
  - Phase 7: Engine 集成 — task_runner/security_auditor 懒加载、17 个浏览器/安全/任务斜杠命令
  - Phase 8: TUI 集成 — BrowserPanel、Ctrl+B 切换、18 个斜杠命令
- **会话自动恢复** — 启动时自动加载最近会话，完整回放所有消息到显示区，像从未关过一样
  - CLI: 启动自动恢复 + `/load` 完整回放
  - TUI: 启动自动恢复 + 历史面板点击加载
- **浏览器工具循环检测** — 三层防御：系统提示引导 → 工具描述约束 → 引擎层重复调用检测
- **CLI 手动滚动** — PageUp/PageDown 浏览历史输出，自动滚动到底部智能恢复

### Fixed
- browser_goto 无限循环 — LLM 反复调用同一工具，三层防御机制彻底解决
- security_auditor 每次调用创建新实例导致 /scan-report 拿不到结果
- 安全扫描结果累积 — 第二次扫描合并第一次结果，每次扫描前 clear
- main.py 119 行重复函数定义（_run_forge/_show_forge_list/_run_forge_remove）
- CLI 输出不自动滚动到底部

## [0.1.1] - 2025-05-17

### Added
- **CLI 全屏布局** — 固定底部输入栏（圆角边框 `╭─╮`），对话历史上方滚动显示
- **实时流式输出** — thinking/tool 调用直接更新在输出区，不再切屏
- **斜杠命令补全菜单** — prompt_toolkit CompletionsMenu 浮层，输入 `/` 自动弹出
- **命令快捷键** — `/q` 退出、`/h` 帮助、`/t` 工具、`/n` 新会话、`/u` 用量、`/m` 模型、`/v` 版本、`/c` 清除
- **双击 Escape 强制中断** — 处理中连按两次 Escape 退出卡死状态
- **Banner 显示版本号和模型名** — 右上角 `v0.1.1`，右下角当前模型
- **CHANGELOG.md** — 版本变更追踪文档

### Fixed
- `/quit` 命令无法退出全屏 app — 补全 `cli.exit()` 调用
- thinking/response 顺序错乱 — finalize_live() 在添加 response 之前固化 thinking
- 日志噪音混入输出 — 流式阶段抑制 litellm/naumi_agent INFO 日志

## [0.1.0] - 2025-05-17

### Added
- **斜杠命令正则匹配自动补全** — CLI 模式使用 prompt_toolkit PromptSession，TUI 模式使用 Textual SuggestFromList
- **TUI `/new` 命令** — 保存当前会话并开始新对话
- **`/version` 命令** — 显示当前版本号
- **Thinking 流式输出** — kimi-for-coding 思考过程实时显示到 CLI
- **`__main__.py` 入口** — 支持 `python -m naumi_agent` 启动
- Phase E-H: 自我审查、自我修改、自我进化、工具锻造
- 热重载模块、生命周期钩子、子 Agent 系统
- 长期记忆 (ChromaDB)、会话持久化 (SQLite)
- 30+ 分析工具（chaos/scale/state/vibe/eval 等）
- MCP 协议客户端集成
- FastAPI REST + WebSocket API
- Textual TUI 界面
- 安全沙箱（Docker 容器隔离）

### Fixed
- kimi-for-coding thinking 输出空白 — rich console 与 spinner 冲突导致中文字符丢失，改用 sys.stdout.write 绕过
- `_is_kimi_thinking_model` 误匹配 — kimi-for-coding 不支持 thinking 参数，缩小匹配范围到 kimi-k2.x/kimi-latest
- TUI InputBar CSS height 导致输入框不可见
- TUI `_set_input_enabled` 类型错误（TextArea → Input）
- CLI `prompt_with_completion()` 缺少 await
- Banner ASCII art 使用 box-drawing 字符在部分终端显示异常
