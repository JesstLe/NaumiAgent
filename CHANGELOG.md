# Changelog

All notable changes to this project will be documented in this file.

## [0.1.72] - 2026-06-01

### Changed
- **Self Evolve 工具边界加固** — 将 `self_evolve` 标记为需要确认的破坏性反思闭环工具，并在质量评估或安全回滚前校验 `target_file`、修改前后内容、说明文本、迭代轮次和 `apply_decision` 类型，避免异常输入触发错误的采纳/回滚决策。

## [0.1.71] - 2026-06-01

### Changed
- **Forge Tool 写盘边界加固** — 将 `forge_tool` 标记为需要确认的破坏性工具，并为描述、显式工具名和 LLM 代码输出增加确定性校验；生成工具的保存、加载和删除现在统一使用安全工具名，拒绝路径片段，避免工具锻造流程写出 `generated/` 边界。

## [0.1.70] - 2026-06-01

### Changed
- **Self Modify 工具边界加固** — 将 `self_modify` 标记为需要确认的破坏性自修改工具，并在进入写盘验证链路前校验 `target_file`、`new_content` 和 `description` 的类型、空值与长度，避免无效或异常大的输入触发源码修改流程。

## [0.1.69] - 2026-06-01

### Changed
- **Pursuit 工具边界加固** — 为 `pursue_goal`、`pursuit_list`、`pursuit_status` 和 `pursuit_resume` 增加只读/需确认/状态变更元数据，并为目标文本和持久化运行 ID 增加确定性输入校验，使长循环目标追踪能力具备更清晰的 Agent 自主调用边界。

## [0.1.68] - 2026-06-01

### Changed
- **Browser Daemon 工具边界加固** — 为 `browser_daemon_*` 工具补充只读/需确认/状态变更元数据，并为任务文本、控制文本、运行列表数量、最大步骤数、handoff 超时和 CDP endpoint 增加确定性夹取与校验，使外部浏览器 daemon 执行链路更适合 Agent 自主调用。

## [0.1.67] - 2026-06-01

### Changed
- **Runtime MCP 连接边界加固** — 为 `runtime_status` 和 `runtime_mcp_connect` 增加工具元数据，将 `runtime_mcp_connect` 标记为需确认的命令型运行时变更，并为 MCP server 名称、args 和 env 增加确定性输入校验，避免注册不稳定命名空间或传入非字符串启动参数。

## [0.1.66] - 2026-06-01

### Changed
- **Web 工具安全加固** — 为 `web_search` 和 `web_fetch` 增加只读工具元数据、查询/长度边界夹取、HTTP(S) URL 规范化、内网/本机/保留地址 SSRF 拦截、重定向后 URL 复验、内容类型检查和中文错误提示，使网络工具具备更清晰的安全执行边界。

## [0.1.65] - 2026-06-01

### Changed
- **Analysis Self-Review 模块拆分** — 将 self-review 的源码目录定位、工具注册计数、裸异常/疑似密钥/类型注解/可变全局状态/日志与 TODO 静态扫描迁移到 `analysis_support.self_review`，主 `analysis.py` 保留工具编排、兼容入口与 LLM Self-Review 增强路径。

## [0.1.64] - 2026-06-01

### Changed
- **Analysis Autopsy 模块拆分** — 将 `/autopsy` 的盲目读取风险、执行迹基础设施、假设验证能力、爆炸半径隔离和 DTS-CHE 就绪度评分迁移到 `analysis_support.autopsy`，主 `analysis.py` 保留工具编排、兼容入口与 LLM Autopsy 增强路径。

## [0.1.63] - 2026-06-01

### Changed
- **Analysis Supervisor 模块拆分** — 将 `/supervisor` 的单体风险检测、Worker 候选识别、守护基础设施盘点、错误隔离质量和守护者树就绪度评分迁移到 `analysis_support.supervisor`，主 `analysis.py` 保留工具编排、兼容入口、多智能体 Supervisor 增强路径与 LLM fallback。

## [0.1.62] - 2026-06-01

### Changed
- **Analysis Watchdog 模块拆分** — 将 `/watchdog` 的原地修改风险、心跳/健康检查、回滚基础设施、隔离级别和不死鸟恢复评分迁移到 `analysis_support.watchdog`，主 `analysis.py` 保留工具编排、兼容入口与 LLM Watchdog 增强路径。

## [0.1.61] - 2026-06-01

### Changed
- **Analysis Cosmos 模块拆分** — 将 `/cosmos` 的状态维度扫描、生成能力识别、社会模拟要素盘点、观测者响应检测和创世潜力评分迁移到 `analysis_support.cosmos`，主 `analysis.py` 保留工具编排、兼容入口与 LLM Cosmos 增强路径。

## [0.1.60] - 2026-06-01

### Changed
- **Analysis Macro 模块拆分** — 将 `/macro` 的中心化瓶颈扫描、数据市场潜力识别、激励机制盘点、竞争淘汰检测和自由市场就绪度评分迁移到 `analysis_support.macro`，主 `analysis.py` 保留工具编排、兼容入口与 LLM Macro 增强路径。

## [0.1.59] - 2026-06-01

### Changed
- **Analysis Genesis 模块拆分** — 将 `/genesis` 的刚性检测、元编程能力盘点、自省能力识别、架构灵活性扫描和自演化就绪度评分迁移到 `analysis_support.genesis`，主 `analysis.py` 保留工具编排、兼容入口与 LLM Genesis 增强路径。

## [0.1.58] - 2026-06-01

### Changed
- **Analysis ZKP 模块拆分** — 将 `/zkp` 的未验证输出检测、引用基础设施盘点、事实-证据缺口分析、验证层识别和可验证计算评分迁移到 `analysis_support.zkp`，主 `analysis.py` 保留工具编排、兼容入口与 LLM ZKP 增强路径。

## [0.1.57] - 2026-06-01

### Changed
- **Analysis PID 模块拆分** — 将 `/pid` 的开环检测、反馈检查点盘点、误差累积风险、预测性纠偏识别和 PID 成熟度评分迁移到 `analysis_support.pid`，主 `analysis.py` 保留工具编排、兼容入口与 LLM PID 增强路径。

## [0.1.56] - 2026-06-01

### Changed
- **Analysis Consensus 模块拆分** — 将 `/consensus` 的高风险决策扫描、单点模型决策识别、多样性/冗余检测和共识架构评分迁移到 `analysis_support.consensus`，主 `analysis.py` 保留工具编排、兼容入口与 LLM Consensus 增强路径。

## [0.1.55] - 2026-06-01

### Changed
- **Analysis Fusion 模块拆分** — 将 `/fusion` 的概率区扫描、精度敏感区识别、危险融合点检测、过度决定论识别和融合架构评分迁移到 `analysis_support.fusion`，主 `analysis.py` 保留工具编排、兼容入口与 LLM Fusion 增强路径。

## [0.1.54] - 2026-06-01

### Changed
- **Analysis World 模块拆分** — 将 `/world` 的状态清单、状态转移映射、因果链分析、客体永久性审计和反事实缺口评分迁移到 `analysis_support.world`，主 `analysis.py` 保留工具编排、兼容入口与 LLM World 增强路径。

## [0.1.53] - 2026-06-01

### Changed
- **Analysis SPAR 模块拆分** — 将 `/spar` 的攻击面扫描、奖励作弊检测、虚无主义检测和自博弈就绪度评分迁移到 `analysis_support.spar`，主 `analysis.py` 保留工具编排、兼容入口与 LLM SPAR 增强路径。

## [0.1.52] - 2026-06-01

### Changed
- **Analysis OODA 模块拆分** — 将 `/ooda` 的脆弱模式扫描、OODA 阶段覆盖分析、韧性评分和确定性任务指挥报告迁移到 `analysis_support.ooda`，主 `analysis.py` 保留工具编排与 LLM OODA 增强路径。

## [0.1.51] - 2026-06-01

### Changed
- **Analysis Sleep 模块收拢** — 将 `/sleep` 的会话主题分布扫描和上下文体积估算迁移到 `analysis_support.sleep`，让 Sleep 的确定性扫描、inventory script 和突触修剪报告集中维护。

## [0.1.50] - 2026-06-01

### Changed
- **Analysis Page 模块收拢** — 将 `/page` 的上下文 Token 粗估、角色分布扫描和分页压力证据生成迁移到 `analysis_support.page`，让 Page 的确定性扫描、inventory script 和报告渲染集中维护。

## [0.1.49] - 2026-06-01

### Changed
- **Analysis COOE 模块拆分** — 将 `/cooe` 的 I/O 阻塞扫描、并行化检测、串行依赖分析、AST 调用图和确定性 DAG/ROB 调度报告迁移到 `analysis_support.cooe`，主 `analysis.py` 保留工具编排与 LLM 架构增强路径。

## [0.1.48] - 2026-06-01

### Changed
- **Analysis Pointer 模块拆分** — 将 `/pointer` 的精密数据模式识别、外部数据源扫描、推理态/物理态边界风险评分和 pointer table 推断迁移到 `analysis_support.pointer`，主 `analysis.py` 保留工具编排与 LLM SPA 架构增强路径。

## [0.1.47] - 2026-06-01

### Changed
- **Analysis Speculate 模块拆分** — 将 `/speculate` 的样板代码识别、高风险区域扫描、文件复杂度分布和双阶段审查报告迁移到 `analysis_support.speculate`，主 `analysis.py` 保留工具编排与 LLM 推测解码增强路径。

## [0.1.46] - 2026-06-01

### Fixed
- **Streaming 工具调用文本缓冲** — 工具可用的流式回合会先缓冲模型文本，避免工具调用前导碎片或参数片段泄露到 CLI/TUI；纯文本回合继续实时输出 token。

## [0.1.45] - 2026-06-01

### Changed
- **Analysis Route 模块拆分** — 将 `/route` 的领域关键词扫描、确定性专家选择、专家冲突解析和 MoE 报告骨架迁移到 `analysis_support.route`，主 `analysis.py` 保留 SubAgent/LLM 执行编排路径。

## [0.1.44] - 2026-06-01

### Changed
- **Analysis MCTS 模块拆分** — 将 `/mcts` 的决策空间扫描、复杂度等级提取和确定性多路径剪枝报告迁移到 `analysis_support.mcts`，主 `analysis.py` 保留工具编排与 LLM 慢思考深化路径。

## [0.1.43] - 2026-06-01

### Changed
- **Analysis GraphRAG 模块拆分** — 将 `/graph` 的 AST 实体关系抽取、循环依赖检测、连通分量和度中心性统计迁移到 `analysis_support.graph`，主 `analysis.py` 保留工具编排与 LLM 图谱推演路径。

## [0.1.42] - 2026-06-01

### Changed
- **Analysis 静态基础模块拆分** — 将 `/chaos`、`/scale`、`/state` 的基础静态扫描、AST 源码读取和通用扫描报告格式化迁移到 `analysis_support.static_modes`，主 `analysis.py` 保留工具编排与 LLM 增强路径。

## [0.1.41] - 2026-06-01

### Changed
- **Analysis Eval 模块拆分** — 将 `/eval` 的 AST 签名扫描、公开函数/类目标提取、可运行 pytest baseline 渲染和确定性报告迁移到 `analysis_support.eval`，主 `analysis.py` 保留目标解析与 LLM 边界测试增强路径。

## [0.1.40] - 2026-06-01

### Changed
- **Analysis Heal 模块拆分** — 将 `/heal` 的错误日志摘要、traceback 切片、错误处理静态扫描和确定性修复建议迁移到 `analysis_support.heal`，主 `analysis.py` 保留目标解析、源码读取和 LLM 热修复增强路径。

## [0.1.39] - 2026-06-01

### Changed
- **Analysis DSPy 模块拆分** — 将 `/dspy` 的 Prompt/Few-shot/Metric 静态扫描、成熟度评分、baseline metric 生成和确定性报告迁移到 `analysis_support.dspy`，主 `analysis.py` 保留工具编排与 LLM 编译建议路径。

## [0.1.38] - 2026-06-01

### Changed
- **Analysis Vibe 模块拆分** — 将 `/vibe` 的可运行 Demo scaffold 生成、文件写入、HTML 转义和请求扫描迁移到 `analysis_support.vibe`，主 `analysis.py` 仅保留工具编排、权限元数据和 LLM 增强路径。

## [0.1.37] - 2026-06-01

### Changed
- **Analysis JIT 模块拆分** — 将 `/jit` 的任务扫描、AST 安全算术求值、可运行脚本渲染和确定性报告迁移到 `analysis_support.jit`，主 `analysis.py` 仅保留工具编排与 LLM 增强路径。

## [0.1.36] - 2026-06-01

### Changed
- **Analysis 熵减模块拆分** — 将 `/entropy` 的确定性扫描、句子去重和三句锚点构造迁移到 `analysis_support.entropy`，主 `analysis.py` 继续保留兼容别名与 Tool 编排。

## [0.1.35] - 2026-06-01

### Added
- **Browser Daemon Watch 闭环** — 新增 `browser_daemon_watch` 工具和 `/bdaemon watch` CLI/TUI 入口，对齐外部 daemon MCP 的 `browser_task_watch` 能力，可等待运行到完成、失败、中止或人工接管/回复状态。

### Fixed
- **Browser Daemon 控制工具可用性** — 控制工具误注册时现在返回中文可控错误，不再暴露 `NotImplementedError`。

## [0.1.34] - 2026-06-01

### Added
- **Self-Evolve 安全执行闭环** — `self_evolve` 新增 `apply_decision`，可在显式开启时记录采纳/迭代状态，并在回滚决策下安全写回原始内容。

### Fixed
- **Self-Evolve 质量评估修复** — 修改前/修改后指标现在都基于对应源码内容的临时文件测量，避免当前目标文件状态污染 before lint 指标；ruff 错误计数兼容新版输出格式。
- **Self-Modify 路径边界修复** — 自修改目标解析改用 `Path.relative_to()` 做结构化边界判断，避免 macOS `/var` 与 `/private/var` 等规范化差异导致合法路径被误判越界。

## [0.1.33] - 2026-06-01

### Fixed
- **Self-Modify 备份安全性** — 自我修改备份改为 `git hash-object` blob 备份，不再执行 `git add/stash/pop`，避免污染用户暂存区或干扰未提交改动。
- **Self-Modify 测试执行可靠性** — 自我修改回归测试移除对 `pytest-timeout` CLI 插件参数的依赖，继续由 subprocess timeout 控制最长执行时间。

## [0.1.32] - 2026-06-01

### Added
- **Forge 确定性锻造能力** — `forge_tool` 在未提供 `llm_output` 时也会生成可运行工具骨架、执行语法/接口/实例化验证并保存到 generated 目录。

### Changed
- **Forge 手动命令回退** — `/forge` 现在优先使用 LLM 生成代码，LLM 失败时会自动回退到底层确定性工具骨架，不再直接中断。

## [0.1.31] - 2026-06-01

### Added
- **分析工具无 Router smoke 覆盖** — 新增 34 个 analysis tools 的统一 smoke 测试，验证无模型路由时全部返回确定性输出且不回退到旧的 `Router 未注入` 文案。

## [0.1.30] - 2026-06-01

### Added
- **Sleep 工具确定性落地** — `/sleep` 在模型路由未初始化时也会返回突触修剪报告、可运行 sleep inventory、保留/修剪候选和 evolution patch contract。

### Fixed
- **Sleep 执行顺序修复** — 突触修剪现在会先扫描会话与源码材料，再按需追加 LLM 增强，避免无模型时跳过确定性压缩能力。

## [0.1.29] - 2026-06-01

### Added
- **Page 工具确定性落地** — `/page` 在模型路由未初始化时也会返回内存分页报告、可运行 page inventory、上下文压力估算和 page_out/page_in contract。

### Changed
- **Page 接口补强** — `analysis_page` 新增可选 `session_context` 参数，用于基于真实 transcript 计算上下文压力。

## [0.1.28] - 2026-06-01

### Added
- **Self-Review 工具确定性落地** — `/self-review` 在模型路由未初始化时也会返回自审查报告、可运行 self-review inventory、AST 代码健康指标和 self-evolution contract。

### Fixed
- **Self-Review 执行顺序修复** — 自审查现在会先定位并扫描源码，再按需追加 LLM 增强，避免无模型时跳过静态自审能力。

## [0.1.27] - 2026-06-01

### Added
- **Autopsy 工具确定性落地** — `/autopsy` 在模型路由未初始化时也会返回执行迹切片审计、可运行 autopsy inventory、AST 调用图、假设/爆炸半径检测和 autopsy contract。

### Fixed
- **Autopsy 源码读取修复** — `_scan_autopsy` 现在会先解析目标路径再读取源码，避免路径字符串被逐字符处理导致扫描为空。

## [0.1.26] - 2026-06-01

### Added
- **Supervisor 工具确定性落地** — `/supervisor` 在模型路由未初始化时也会返回守护者树审计、可运行 supervisor inventory、Worker/守护/隔离检测和 restart contract。

### Fixed
- **Supervisor 源码读取修复** — `_scan_supervisor` 现在会先解析目标路径再读取源码，避免路径字符串被逐字符处理导致扫描为空。

## [0.1.25] - 2026-06-01

### Added
- **Cosmos 工具确定性落地** — `/cosmos` 在模型路由未初始化时也会返回创世引擎审计、可运行 cosmos inventory、状态/生成/社会/观测检测和 genesis contract。

## [0.1.24] - 2026-06-01

### Added
- **Watchdog 工具确定性落地** — `/watchdog` 在模型路由未初始化时也会返回灾难隔离审计、可运行 watchdog inventory、健康检查/回滚/隔离检测和 phoenix contract。

### Fixed
- **Watchdog 源码读取修复** — `_scan_watchdog` 现在会先解析目标路径再读取源码，避免路径字符串被逐字符处理导致扫描为空。
- **Cosmos 源码读取预修复** — `_scan_cosmos` 现在会先解析目标路径再读取源码，为后续确定性落地扫清空扫描问题。

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
