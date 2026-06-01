# Changelog

All notable changes to this project will be documented in this file.

## [0.1.23] - 2026-06-01

### Added
- **Macro 工具确定性落地** — `/macro` 在模型路由未初始化时也会返回多智能体市场审计、可运行 market inventory、中心化/数据市场/激励/竞争检测和 market contract。

### Fixed
- **Macro 源码读取修复** — `_scan_macro` 现在会先解析目标路径再读取源码，避免路径字符串被逐字符处理导致扫描为空。

## [0.1.22] - 2026-06-01

### Added
- **Genesis 工具确定性落地** — `/genesis` 在模型路由未初始化时也会返回自演化审计、可运行 genesis inventory 脚本、刚性/元编程/自省检测和 evolution contract。

### Fixed
- **Genesis 源码读取修复** — `_scan_genesis` 现在会先解析目标路径再读取源码，避免路径字符串被逐字符处理导致扫描为空。

## [0.1.21] - 2026-06-01

### Added
- **ZKP 工具确定性落地** — `/zkp` 在模型路由未初始化时也会返回轨迹校验方案、可运行 trace verifier、不可验证输出检测和 trace contract。

### Fixed
- **ZKP 源码读取修复** — `_scan_zkp` 现在会先解析目标路径再读取源码，避免路径字符串被逐字符处理导致扫描为空。

## [0.1.20] - 2026-06-01

### Added
- **PID 工具确定性落地** — `/pid` 在模型路由未初始化时也会返回闭环控制审计、可运行 PID inventory 脚本、P/I/D 改造契约和渐进实施计划。

### Fixed
- **PID 源码读取修复** — `_scan_pid` 现在会先解析目标路径再读取源码，避免路径字符串被逐字符处理导致扫描为空。

## [0.1.19] - 2026-06-01

### Added
- **Consensus 工具确定性落地** — `/consensus` 在模型路由未初始化时也会返回共识审计、可运行 consensus inventory 脚本、高风险/单点决策检测和 quorum 契约。

### Fixed
- **Consensus 源码读取修复** — `_scan_consensus` 现在会先解析目标路径再读取源码，避免路径字符串被逐字符处理导致扫描为空。

## [0.1.18] - 2026-06-01

### Added
- **Fusion 工具确定性落地** — `/fusion` 在模型路由未初始化时也会返回概率/决定论边界审计、可运行 fusion inventory 脚本、危险融合点和验证层契约。

### Fixed
- **Fusion 源码读取修复** — `_scan_fusion` 现在会先解析目标路径再读取源码，避免路径字符串被逐字符处理导致扫描为空。

## [0.1.17] - 2026-06-01

### Added
- **World 工具确定性落地** — `/world` 在模型路由未初始化时也会返回世界模型审计、可运行状态 inventory 脚本、状态宇宙图谱和反事实补强计划。

### Fixed
- **AST 扫描源码读取修复** — 新增 AST 安全源码读取路径，避免 `### path` 头部破坏 Python AST；`/world` 与 `/spar` 的状态/空函数扫描现在能读取真实源码。

## [0.1.16] - 2026-06-01

### Added
- **SPAR 工具确定性落地** — `/spar` 在模型路由未初始化时也会返回对抗自博弈基线、可运行静态 adversarial harness、红队测试建议和收敛门槛。

### Fixed
- **SPAR 源码读取修复** — `_scan_spar` 现在会先解析目标路径再读取源码，避免把路径字符串逐字符当作文件列表导致扫描为空。
- **SPAR 误报收敛** — 堆释放模式改为匹配真实 `free()`/`delete` 调用，避免将 `dependency-free` 等普通文本误判为内存释放。

## [0.1.15] - 2026-06-01

### Added
- **Vision 工具确定性落地** — `/vision` 在模型路由未初始化时也会返回视觉提取方案、截图 inventory 脚本、ROI 合约模板和字段校验计划。
- **Vision 模块拆分** — 将 `/vision` 的反爬障碍检测、数据类型识别、管线规划和截图脚本生成抽到 `analysis_support.vision`，继续压缩万行分析模块。

## [0.1.14] - 2026-06-01

### Added
- **Hook 工具确定性落地** — `/hook` 在模型路由未初始化时也会返回合规只读侦测方案、目标平台/保护风险扫描、可运行 inventory 脚本和回填模板。
- **Hook 模块拆分** — 将 `/hook` 的目标分类、保护风险检测、inventory 脚本生成和报告生成抽到 `analysis_support.hook`，继续缩小万行 `analysis.py`。

## [0.1.13] - 2026-06-01

### Added
- **Probe 工具确定性落地** — `/probe` 在模型路由未初始化时也会返回只读黑盒探测协议、可运行 Python 探测脚本、信息回填模板和基于真实证据的后续开发计划。
- **Probe 模块拆分起步** — 将 `/probe` 的静态风险扫描、模式选择、脚本生成和报告生成抽到 `analysis_support.probe`，保留 `analysis.py` 兼容入口，降低万行分析模块继续膨胀的风险。

## [0.1.12] - 2026-06-01

### Added
- **OODA 工具确定性落地** — `/ooda` 在模型路由未初始化时也会返回 Observe/Orient/Decide/Act 循环设计、自愈机制、抗脆弱检查清单和韧性评分。

## [0.1.11] - 2026-06-01

### Added
- **COOE 工具确定性落地** — `/cooe` 在模型路由未初始化时也会生成 DAG 任务拆解、Reservation Stations、ROB 配置、顺序/并行耗时估算和加速比。

## [0.1.10] - 2026-06-01

### Added
- **Pointer 工具确定性落地** — `/pointer` 在模型路由未初始化时也会返回 SPA 推理态/物理态分离方案、Pointer Table、迁移计划和风险扫描结果。

## [0.1.9] - 2026-06-01

### Added
- **Speculate 工具确定性落地** — `/speculate` 在模型路由未初始化时也会返回 Intern Draft 与 Architect Review 双阶段计划、风险文件列表和 diff 验证契约。

## [0.1.8] - 2026-06-01

### Added
- **MoE Route 工具确定性落地** — `/route` 在模型路由未初始化时也会根据任务和代码关键词生成 3-5 位专家小组、跨专家冲突处理、综合行动计划和资源估算。

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
