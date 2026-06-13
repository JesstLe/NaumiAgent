# Changelog

All notable changes to this project will be documented in this file.

## [0.1.173] - 2026-06-13

### Fixed
- **Self-Modify Malformed Validation Guard** — `self_modify` 现在会把畸形验证结果转换为结构化拒绝报告，避免验证条目缺少 `passed` 等字段时让自我修改工具调用直接崩溃。

## [0.1.172] - 2026-06-13

### Fixed
- **Self-Evolve Malformed Cycle Guard** — `self_evolve` 现在会把畸形的反思循环结果转换为结构化拒绝报告，避免内部评估结果缺字段时让 Agent 工具调用直接崩溃。

## [0.1.171] - 2026-06-13

### Fixed
- **Self-Evolve String Number Round** — `self_evolve` 现在接受 `"1"` / `"2"` / `"3"` 形式的 `round` 参数，让 Agent 工具调用轻微错型时仍能进入正确的反思轮次，并继续拒绝越界或非数字轮次。

## [0.1.170] - 2026-06-13

### Fixed
- **Self-Modify String Boolean Workspace Apply** — `self_modify` 现在接受 `"true"` / `"false"` 形式的 `apply_to_workspace` 参数，让 Agent 工具调用轻微错型时仍能正确选择写回或仅验证。

## [0.1.169] - 2026-06-13

### Fixed
- **Self-Evolve String Boolean Apply Decision** — `self_evolve` 现在接受 `"true"` / `"false"` 形式的 `apply_decision` 参数，让 Agent 工具调用轻微错型时仍能正确执行或跳过反思闭环动作。

## [0.1.168] - 2026-06-13

### Fixed
- **CLI Evolve Rollback Payload Guard** — `/evolve` 现在会校验 `self_evolve` 回滚结果中的 `apply_result` 必须是对象，避免工具返回畸形结构时导致自我进化流程崩溃。

## [0.1.167] - 2026-06-13

### Fixed
- **Self-Evolve String Boolean Return Mode** — `self_evolve` 现在接受 `"true"` / `"false"` 形式的 `return_json` 参数，让反思评估工具在 LLM 调用轻微错型时仍能稳定返回期望格式。

## [0.1.166] - 2026-06-13

### Fixed
- **Self-Modify String Boolean Return Mode** — `self_modify` 现在接受 `"true"` / `"false"` 形式的 `return_json` 参数，提升 LLM 工具调用轻微错型时的结构化结果稳定性。

## [0.1.165] - 2026-06-13

### Fixed
- **CLI Evolve Rejected Modify Reporting** — `/evolve` 现在把 `self_modify` 的 `rejected` 结果展示为“自我修改已拒绝”，不再误标为“修改未通过验证”。

## [0.1.164] - 2026-06-13

### Fixed
- **CLI Evolve No-Op Reporting** — `/evolve` 现在把 `self_modify` 的 `noop` 结果展示为“无变更，已停止自我进化”，不再误标为“修改未通过验证”。

## [0.1.163] - 2026-06-13

### Fixed
- **CLI Evolve Rejected Evaluation Handling** — `/evolve` 现在识别 `self_evolve` 的 `rejected` 决策，会展示反思评估报告并停止流程，而不是把合法拒绝结果当成未知动作。

## [0.1.162] - 2026-06-13

### Fixed
- **Structured Self-Evolve Rejections** — `self_evolve(return_json=True)` 现在在输入被拒绝时也返回 `{report, cycle_result}` JSON，避免反思评估工具的结构化调用在错误路径收到 Markdown。

## [0.1.161] - 2026-06-13

### Fixed
- **Structured Self-Modify Rejections** — `self_modify(return_json=True)` 现在在输入被拒绝时也返回 `{report, result}` JSON，避免 Agent/CLI 结构化调用在错误路径收到 Markdown 后解析失败。

## [0.1.160] - 2026-06-13

### Fixed
- **CLI Evolve Candidate-Bound Proposal Prompt** — `/evolve` 现在明确要求 LLM 的 `target_file` 必须来自可修改文件列表，减少 proposal 指向受保护或不存在路径后再被执行阶段拒绝的无效自我进化回合。

## [0.1.159] - 2026-06-13

### Fixed
- **CLI Evolve Snake Case Context Matching** — `/evolve` 的源码上下文排序现在会把下划线代码标识拆成子词，使 `self review` 这类自然写法也能命中 `self_review.py` 等相关模块。

## [0.1.158] - 2026-06-13

### Fixed
- **CLI Evolve Relevant Source Context** — `/evolve` 现在按请求描述中的代码关键词优先选择源码上下文，让 LLM 在修改嵌套工具时先看到相关实现，而不只是排序靠前的文件。

## [0.1.157] - 2026-06-13

### Fixed
- **CLI Evolve Recursive Candidate Discovery** — `/evolve` 现在递归列出 `tools`、`memory`、`skills` 下的可修改 Python 模块，并保持确定性排序，让自我进化能发现嵌套工具实现而不只看到顶层文件。

## [0.1.156] - 2026-06-13

### Fixed
- **CLI Evolve Target Boundary Guard** — `/evolve` 现在在读取原始文件和调用 `self_modify` 前拒绝受保护或不可修改目标，复用自我修改工具的边界规则，避免 LLM proposal 绕进受限源码域。

## [0.1.155] - 2026-06-13

### Fixed
- **CLI Evolve Proposal Shape Guard** — `/evolve` 现在校验 LLM 修改方案必须是 JSON 对象，并要求 `target_file`、`new_content` 与 `description` 为字符串，避免非对象或错型字段绕进自我修改流程。

## [0.1.154] - 2026-06-13

### Fixed
- **Structured Self-Evolve Result Guard** — `/evolve` 现在校验 `self_evolve` 返回的 `cycle_result` 与报告类型，并拒绝未知 action，避免格式异常时崩溃或把未知决策误当作采纳。

## [0.1.153] - 2026-06-13

### Fixed
- **Structured Self-Modify Evolve Gate** — `self_modify` 现在支持结构化 JSON 返回，`/evolve` 通过 `result.status == "applied"` 判断是否进入反思评估，避免被中文报告文本中的“已应用”字样误导。

## [0.1.152] - 2026-06-13

### Fixed
- **CLI Evolve Evaluator Tool Boundary** — `/evolve` 的反思评估现在通过 `self_evolve` ToolCall 执行，并使用结构化 JSON 返回读取决策，确保评估、回滚与阻断原因都进入 Engine 权限、Hook 和审计链。

## [0.1.151] - 2026-06-13

### Fixed
- **CLI Evolve Requires Reflective Evaluator** — `/evolve` 现在要求 `self_modify` 与 `self_evolve` 同时注册；缺少反思评估工具时会在生成/写入前停止，避免只修改不评估的半闭环进化。

## [0.1.150] - 2026-06-13

### Fixed
- **CLI Evolve Safe Rollback Path** — `/evolve` 现在通过 `run_evolution_cycle(apply_decision=True)` 复用 `self_evolve` 的安全闭环与阻断原因，不再绕过该逻辑直接 `git checkout` 回滚目标文件。

## [0.1.149] - 2026-06-13

### Fixed
- **Self-Evolve Rollback Blocker Propagation** — 当 `apply_decision=True` 但安全闭环拒绝自动回滚时，`run_evolution_cycle` 现在会把底层阻断原因传给上层消息，避免 Agent 误以为只是普通“尚未执行”状态。

## [0.1.148] - 2026-06-13

### Fixed
- **Self-Review Plan Mode Access** — `self_review` 现在显式声明为只读工具，Plan 模式下可以先审查自身源码与自进化候选，再决定是否进入写入/进化流程。

## [0.1.147] - 2026-06-13

### Fixed
- **Self-Evolve Rollback Reporting** — `self_evolve` 现在只在闭环实际写回成功时报告“已回滚”；默认只评估不应用时会明确提示“建议回滚，尚未执行”，避免反思循环误判状态。

## [0.1.146] - 2026-06-13

### Fixed
- **Self-Modify Test Evidence Gate** — 自我修改验证现在要求目标模块存在并通过对应 pytest 文件；缺少测试或 pytest 不可用都会拒绝修改，避免自进化在没有行为证据时误判为已验证。

## [0.1.145] - 2026-06-13

### Fixed
- **Self-Evolve Workspace Application Contract** — `self_modify` 现在显式暴露 `apply_to_workspace` 布尔开关，默认仍只做隔离验证；用户主动触发的 `/evolve` 会通过 Engine 写入主工作区，避免把隔离验证成功误判为“未通过验证”。

## [0.1.144] - 2026-06-11

### Fixed
- **Browser Daemon Completion Description** — `/bdaemon` 的旧 CLI 与新 CLI 补全描述现在完整列出 `reply/resume/abort/manual` 控制子命令，避免用户只从补全中误以为 daemon 只能查询到 `watch`。

## [0.1.143] - 2026-06-11

### Fixed
- **Managed Worktree Permission Scope** — Engine 现在会把自身管理的 `worktrees` 存储目录加入权限允许目录，确保 `/pursue` 重定位到隔离 worktree 的 file/bash 路径继续通过 `PermissionChecker` 审计而不会被误判为越界。
- **Version Source Alignment** — API app、health endpoint 与 `pyproject.toml` 现在对齐运行时 `naumi_agent.__version__`，避免 OpenAPI、健康检查和包元数据展示不同版本。

## [0.1.142] - 2026-06-11

### Changed
- **Pursuit File Read Worktree Scope** — `/pursue` 的 `file_read` generic tool 参数现在会在已有 `worktree_path` 时重定位 `path`，拒绝逃逸 worktree 的读取路径，并保留 `offset`、`limit` 等其他参数。

## [0.1.141] - 2026-06-11

### Changed
- **Pursuit File Edit Worktree Scope** — `/pursue` 已创建隔离 worktree 时，`file_edit` 会在 worktree 内读取目标文件并把同一个重定位路径传给 `file_edit` ToolCall，避免从主工作区读取内容再编辑隔离区文件的错配。

## [0.1.140] - 2026-06-11

### Changed
- **Pursuit File Write Worktree Scope** — `/pursue` 已创建隔离 worktree 时，`file_write` 新文件写入会把相对路径重定位到 `worktree_path`，并拒绝 `..` 等逃逸 worktree 的路径，继续通过 Engine 与权限检查执行。

## [0.1.139] - 2026-06-11

### Changed
- **Pursuit Verification Worktree Scope** — `/pursue` 已创建隔离 worktree 时，成功标准验证命令与状态取证命令现在同样携带 `cwd=worktree_path` 执行，使执行、验证、取证的 bash 路径保持在同一个隔离工作区。

## [0.1.138] - 2026-06-11

### Changed
- **Pursuit Bash Worktree Scope** — `/pursue` 已创建隔离 worktree 时，普通 `bash_run` 动作现在会携带 `cwd=worktree_path` 执行，避免命令动作默认落回主工作区；无 worktree 时保持原参数形状不变。

## [0.1.137] - 2026-06-11

### Fixed
- **Scale Slash Argument Contract** — 将 `/scale` 的 AGENTS 协议、CLI help 与补全参数提示统一为 `[目标|QPS]`，匹配实际支持“纯数字作为 QPS，否则作为分析目标”的分发行为。

## [0.1.136] - 2026-06-11

### Fixed
- **Plan Mode Read-Only Tool Allowlist** — 修正 Plan 模式只读工具白名单中的漂移工具名，允许当前真实的 `background_list`、`background_read_output`、`schedule_list`、`worktree_status`，避免状态查看类命令被误判为写操作。

## [0.1.135] - 2026-06-11

### Changed
- **Browser Daemon Slash Execution Boundary** — `/bdaemon health/start/dashboard/run/list/status/watch/reply/resume/abort/manual` 现在优先通过 Engine `_execute_tool` 执行对应 `browser_daemon_*` 工具，保留无执行器场景的直接调用回退，使浏览器 daemon 控制入口进入权限、Hook 和审计链。

## [0.1.134] - 2026-06-11

### Changed
- **Evolve Self-Modify Execution Boundary** — `/evolve` 应用自我修改时现在优先通过 Engine `_execute_tool` 执行 `self_modify`，保留无执行器场景的直接调用回退，使自我修改写文件阶段进入权限、Hook 和审计链。

## [0.1.133] - 2026-06-11

### Changed
- **Forge Slash Execution Boundary** — `/forge` 保存与验证生成工具时现在优先通过 Engine `_execute_tool` 执行 `forge_tool`，保留无执行器场景的直接调用回退，使工具锻造入口进入权限、Hook 和审计链。

## [0.1.132] - 2026-06-11

### Changed
- **Self-Review Slash Execution Boundary** — `/self-review` 现在优先通过 Engine `_execute_tool` 执行 `self_review`，保留无执行器场景的直接调用回退，使自我审查命令进入权限、Hook 和审计链。

## [0.1.131] - 2026-06-11

### Changed
- **Schedule Slash Execution Boundary** — `/schedule create/list/cancel/pause/resume` 现在优先通过 Engine `_execute_tool` 执行对应的 `schedule_*` 工具，保留无执行器场景的直接调用回退，使调度提醒命令进入权限、Hook 和审计链。

## [0.1.130] - 2026-06-11

### Changed
- **Worktree Slash Execution Boundary** — `/worktree status/create/bind/keep/remove` 现在优先通过 Engine `_execute_tool` 执行对应的 `worktree_*` 工具，保留无执行器场景的直接调用回退，使隔离执行区命令进入权限、Hook 和审计链。

## [0.1.129] - 2026-06-11

### Changed
- **Background Slash Execution Boundary** — `/background run/status/list/cancel/cleanup/output` 现在优先通过 Engine `_execute_tool` 执行对应的 `background_*` 工具，保留无执行器场景的直接调用回退，使后台任务命令进入权限、Hook 和审计链。

## [0.1.128] - 2026-06-11

### Changed
- **Pursue Meta Execution Boundary** — `/pursue list/status/resume` 现在优先通过 Engine `_execute_tool` 执行对应的 `pursuit_*` 工具，保留无执行器场景的直接调用回退，使目标追踪状态命令同样进入权限、Hook 和审计链。

## [0.1.127] - 2026-06-11

### Changed
- **Pursue Slash Execution Boundary** — `/pursue <目标>` 现在优先通过 Engine `_execute_tool` 执行 `pursue_goal`，保留无执行器场景的直接调用回退，使目标追踪启动入口进入权限、Hook 和审计链。

## [0.1.126] - 2026-06-11

### Changed
- **Main Analysis Execution Boundary** — 主 CLI `_run_analysis` 在 Engine 暴露 `_execute_tool` 时改为构造 `ToolCall` 并以 `agent_name="cli"` 进入统一执行器，保留无执行器场景的直接调用回退，避免手动分析命令绕过权限、Hook 和审计链。

## [0.1.125] - 2026-06-11

### Fixed
- **Page Analysis CLI Context** — 主 CLI `_run_analysis(..., "page", target)` 现在会把输入上下文传给 `analysis_page` 的 `session_context`，与共享分析命令入口保持一致，避免 `/page` 在不同入口下丢失上下文。

## [0.1.124] - 2026-06-11

### Fixed
- **Slash Completion Optional Args** — 对齐 `/chaos [目标]`、`/scale [QPS]`、`/dspy [描述]`、`/graph [路径]` 的 canonical 补全 metadata，并同步旧兼容补全的参数标记。

## [0.1.123] - 2026-06-11

### Changed
- **Shared Analysis Execution Boundary** — `cli.commands_analysis.run_analysis` 在 Engine 暴露 `_execute_tool` 时改为构造 `ToolCall` 并以 `agent_name="cli"` 进入统一执行器，使复用共享分析命令的入口不再绕过权限、Hook 和审计链；同时对齐 `/scale [QPS]` 的共享参数解析，保留无执行器场景的兼容回退。

## [0.1.122] - 2026-06-11

### Fixed
- **Shared Analysis Command Dispatch** — 修复 `cli.commands_analysis.run_analysis` 仍使用旧分析 Tool 名称的问题，并统一非 `target` 参数构造，避免复用共享分析命令模块的入口报“工具未注册”或传错参数。

## [0.1.121] - 2026-06-11

### Changed
- **Pursuit Background Result Boundary** — `/pursue` 回收后台任务状态与输出时优先通过注入的 Engine ToolCall 执行 `background_status` 和 `background_read_output`，保留本地回退，避免后台证据回收绕过权限、Hook 和审计链。

## [0.1.120] - 2026-06-11

### Changed
- **Pursuit Evidence Execution Boundary** — `/pursue` 状态证据采集中的 diff、文件快照、pytest 和 ruff 命令优先通过注入的 Engine ToolCall 执行 `bash_run`，保留本地工具回退，避免证据采集绕过权限、Hook 和审计链。

## [0.1.119] - 2026-06-11

### Changed
- **Pursuit Worktree Execution Boundary** — `/pursue` 自动创建隔离 worktree 时优先通过注入的 Engine ToolCall 执行 `worktree_create`，保留无注入执行器时的本地回退，避免绕过权限、Hook 和审计链。

## [0.1.118] - 2026-06-11

### Added
- **Session Delete Tool** — 新增 `session_delete` Tool，将 `/delete` 的历史会话删除能力暴露给 Agent 自主调用，并声明 `destructive` 与确认要求；工具只接受明确会话 ID，避免数字编号误删。

## [0.1.117] - 2026-06-11

### Added
- **Session Load Tool** — 新增 `session_load` Tool，将 `/load` 的历史会话恢复能力暴露给 Agent 自主调用，支持会话 ID 和最近列表编号加载，继续补齐会话命令双通道能力。

## [0.1.116] - 2026-06-11

### Added
- **Session History Tool** — 新增只读 `session_history` Tool，将 `/history` 的历史会话列表与预览能力暴露给 Agent 自主调用，并在 Engine 工具注册表中注册，为会话命令补齐双通道能力的第一步。

## [0.1.110] - 2026-06-01

### Changed
- **Vibe Analysis Tool 拆分** — 将 `analysis_vibe` 的 Tool 包装迁移到 `analysis_tools.vibe`，保留 `analysis.py` 兼容 wrapper、`destructive`/`output_dir` 路径 metadata 和 slash/Agent 共用 execute 路径；新增注入式 runner 写文件测试，覆盖真实 scaffold 落盘、权限声明、无 router 确定性输出和 LLM 增强建议路径。

## [0.1.109] - 2026-06-01

### Changed
- **State Analysis Tool 拆分** — 将 `analysis_state` 的 Tool 包装迁移到 `analysis_tools.state`，保留 `analysis.py` 兼容 wrapper 和 slash/Agent 共用 execute 路径；新增注入式 runner 测试，覆盖真实有状态违规扫描、部署 context 传递、无 router 确定性输出和 LLM 分布式改造增强路径。

## [0.1.108] - 2026-06-01

### Changed
- **Scale Analysis Tool 拆分** — 将 `analysis_scale` 的 Tool 包装迁移到 `analysis_tools.scale`，保留 `analysis.py` 兼容 wrapper 和 slash/Agent 共用 execute 路径；新增注入式 runner 测试，覆盖目标 QPS 参数传递、真实高并发瓶颈扫描、无 router 确定性输出和 LLM 扩容方案增强路径。

## [0.1.107] - 2026-06-01

### Changed
- **Chaos Analysis Tool 拆分** — 参照 Claude Code 的工具执行流水线模式，将 `analysis_chaos` 的 Tool 包装迁移到 `analysis_tools.chaos`，保留 `analysis.py` 兼容 wrapper 和 slash/Agent 共用 execute 路径；新增注入式 runner 测试，覆盖真实静态 SPOF 扫描、无 router 确定性输出和 LLM 灾难推演增强路径。

## [0.1.106] - 2026-06-01

### Changed
- **Self-Review Analysis Tool 拆分** — 将 `self_review` 的 Tool 包装迁移到 `analysis_tools.self_review`，保留 `analysis.py` 兼容 wrapper、全局 router 行为和源码目录定位注入点；新增注入式 runner 测试，覆盖源码自审扫描、inventory 脚本执行、Self-Evolution contract 和 LLM Self-Review 增强路径。

## [0.1.105] - 2026-06-01

### Changed
- **Autopsy Analysis Tool 拆分** — 将 `analysis_autopsy` 的 Tool 包装迁移到 `analysis_tools.autopsy`，保留 `analysis.py` 兼容 wrapper 和全局 router 行为；新增注入式 runner 测试，覆盖 DTS-CHE 静态扫描、执行迹/假设验证/爆炸半径检测、真实 autopsy inventory 脚本执行和 LLM Autopsy 增强路径。

## [0.1.104] - 2026-06-01

### Changed
- **Supervisor Analysis Tool 拆分** — 将 `analysis_supervisor` 的 Tool 包装迁移到 `analysis_tools.supervisor`，保留 `analysis.py` 兼容 wrapper、全局 router 行为和 SubAgent manager 守护树路径；新增注入式 runner 测试，覆盖单体风险扫描、Worker 候选/守护者基础设施/错误隔离检测、真实 supervisor inventory 脚本执行和 LLM Supervisor 增强路径。

## [0.1.103] - 2026-06-01

### Changed
- **Watchdog Analysis Tool 拆分** — 将 `analysis_watchdog` 的 Tool 包装迁移到 `analysis_tools.watchdog`，保留 `analysis.py` 兼容 wrapper 和全局 router 行为；新增注入式 runner 测试，覆盖原地修改风险扫描、心跳/回滚/隔离机制检测、真实 watchdog inventory 脚本执行和 LLM Watchdog 增强路径。

## [0.1.102] - 2026-06-01

### Changed
- **Cosmos Analysis Tool 拆分** — 将 `analysis_cosmos` 的 Tool 包装迁移到 `analysis_tools.cosmos`，保留 `analysis.py` 兼容 wrapper 和全局 router 行为；新增注入式 runner 测试，覆盖状态维度/生成能力/社会模拟/观测响应扫描、真实 cosmos inventory 脚本执行和 LLM Cosmos 增强路径。

## [0.1.101] - 2026-06-01

### Changed
- **Macro Analysis Tool 拆分** — 将 `analysis_macro` 的 Tool 包装迁移到 `analysis_tools.macro`，保留 `analysis.py` 兼容 wrapper 和全局 router 行为；新增注入式 runner 测试，覆盖中心化决策扫描、数据市场/激励/竞争机制检测、真实 market inventory 脚本执行和 LLM Macro 增强路径。

## [0.1.100] - 2026-06-01

### Changed
- **Genesis Analysis Tool 拆分** — 将 `analysis_genesis` 的 Tool 包装迁移到 `analysis_tools.genesis`，保留 `analysis.py` 兼容 wrapper 和全局 router 行为；新增注入式 runner 测试，覆盖刚性代码扫描、元编程/自省能力检测、真实 self-evolution inventory 脚本执行和 LLM Genesis 增强路径。

## [0.1.99] - 2026-06-01

### Changed
- **ZKP Analysis Tool 拆分** — 将 `analysis_zkp` 的 Tool 包装迁移到 `analysis_tools.zkp`，保留 `analysis.py` 兼容 wrapper 和全局 router 行为；新增注入式 runner 测试，覆盖未验证 AI 输出扫描、引用基础设施检测、真实 trace verifier 脚本执行和 LLM ZKP 增强路径。

## [0.1.98] - 2026-06-01

### Changed
- **PID Analysis Tool 拆分** — 将 `analysis_pid` 的 Tool 包装迁移到 `analysis_tools.pid`，保留 `analysis.py` 兼容 wrapper 和全局 router 行为；新增注入式 runner 测试，覆盖开环流水线扫描、误差累积风险检测、真实 pid inventory 脚本执行和 LLM PID 增强路径。

## [0.1.97] - 2026-06-01

### Changed
- **Consensus Analysis Tool 拆分** — 将 `analysis_consensus` 的 Tool 包装迁移到 `analysis_tools.consensus`，保留 `analysis.py` 兼容 wrapper 和全局 router 行为；新增注入式 runner 测试，覆盖高风险决策扫描、单模型决策点检测、真实 consensus inventory 脚本执行和 LLM Consensus 增强路径。

## [0.1.96] - 2026-06-01

### Changed
- **Fusion Analysis Tool 拆分** — 将 `analysis_fusion` 的 Tool 包装迁移到 `analysis_tools.fusion`，保留 `analysis.py` 兼容 wrapper 和全局 router 行为；新增注入式 runner 测试，覆盖 AI/LLM 调用扫描、危险融合点检测、真实 fusion inventory 脚本执行和 LLM Fusion 增强路径。

## [0.1.95] - 2026-06-01

### Changed
- **World Analysis Tool 拆分** — 将 `analysis_world` 的 Tool 包装迁移到 `analysis_tools.world`，保留 `analysis.py` 兼容 wrapper 和全局 router 行为；新增注入式 runner 测试，覆盖 AST 状态转移扫描、客体永久性审计、真实 world inventory 脚本执行和 LLM World 增强路径。

## [0.1.94] - 2026-06-01

### Changed
- **SPAR Analysis Tool 拆分** — 将 `analysis_spar` 的 Tool 包装迁移到 `analysis_tools.spar`，保留 `analysis.py` 兼容 wrapper、全局 router 行为和 SubAgent manager 对抗路径；新增注入式 runner 测试，覆盖静态攻击面扫描、奖励作弊检测、真实 harness 脚本执行和 LLM SPAR 增强路径。
- **SPAR 调度依赖注入化** — 将 SubAgent manager 获取逻辑从工具主体中抽出为 `subagent_manager_getter` 注入点，便于后续替换蓝军/红军执行器并单测真实对抗循环。

## [0.1.93] - 2026-06-01

### Changed
- **Vision Analysis Tool 拆分** — 将 `analysis_vision` 的 Tool 包装迁移到 `analysis_tools.vision`，保留 `analysis.py` 兼容 wrapper 和全局 router 行为；新增注入式 runner 测试，覆盖视觉任务扫描、真实 PNG inventory 脚本执行、ROI 提取契约和 LLM Vision 增强路径。
- **Vision 合规边界收紧** — 将 LLM 增强 prompt 从“绕过反爬限制”调整为授权页面/用户截图/可见样本的证据优先视觉提取协议；遇到登录、验证码、WAF、访问控制等场景时只做授权采集与合规审查规划。

## [0.1.92] - 2026-06-01

### Changed
- **Hook Analysis Tool 拆分** — 将 `analysis_hook` 的 Tool 包装迁移到 `analysis_tools.hook`，保留 `analysis.py` 兼容 wrapper 和全局 router 行为；新增注入式 runner 测试，覆盖逆向/插桩目标扫描、确定性 Hook 报告、真实只读 inventory 脚本和 LLM Hook 增强路径。
- **Hook 合规边界收紧** — 将 LLM 增强 prompt 从“反调试规避方案”调整为授权、只读、证据优先的插桩侦测协议；遇到反作弊、完整性校验、内核驱动等风险时只做风险识别并要求授权确认。

## [0.1.91] - 2026-06-01

### Changed
- **Probe Analysis Tool 拆分** — 将 `analysis_probe` 的 Tool 包装迁移到 `analysis_tools.probe`，保留 `analysis.py` 兼容 wrapper 和全局 router 行为；新增注入式 runner 测试，继续覆盖只读探测脚本生成、真实脚本执行、信息回填模板和 LLM 探测增强路径。

## [0.1.90] - 2026-06-01

### Changed
- **OODA Analysis Tool 拆分** — 将 `analysis_ooda` 的 Tool 包装迁移到 `analysis_tools.ooda`，保留 `analysis.py` 兼容 wrapper、目标解析和全局 router 行为；新增真实脆弱自动化源码的注入式测试，覆盖硬编码等待/WebDriver 定位扫描、确定性 OODA 指挥方案和 LLM OODA 增强路径。

## [0.1.89] - 2026-06-01

### Changed
- **COOE Analysis Tool 拆分** — 将 `analysis_cooe` 的 Tool 包装迁移到 `analysis_tools.cooe`，保留 `analysis.py` 兼容 wrapper、可选源码目标扫描和全局 router 行为；新增真实 async pipeline 的注入式测试，覆盖 I/O/调用图/ROB 扫描、确定性 DAG 调度和 LLM COOE 架构增强路径。

## [0.1.88] - 2026-06-01

### Changed
- **Pointer Analysis Tool 拆分** — 将 `analysis_pointer` 的 Tool 包装迁移到 `analysis_tools.pointer`，保留 `analysis.py` 兼容 wrapper、目标解析和全局 router 行为；新增真实金融报价/HTTP 数据源的注入式测试，覆盖精密数据与外部边界扫描、确定性 SPA 指针表和 LLM SPA 架构增强路径。

## [0.1.87] - 2026-06-01

### Changed
- **Speculate Analysis Tool 拆分** — 将 `analysis_speculate` 的 Tool 包装迁移到 `analysis_tools.speculate`，保留 `analysis.py` 兼容 wrapper、目标解析和全局 router 行为；新增真实高风险源码的注入式测试，覆盖子进程执行风险扫描、确定性双阶段计划和 LLM 推测解码增强路径。

## [0.1.86] - 2026-06-01

### Changed
- **Route Analysis Tool 拆分** — 将 `analysis_route` 的 Tool 包装迁移到 `analysis_tools.route`，保留 `analysis.py` 兼容 wrapper、SubAgent manager stale-router 保护和 LLM fallback 行为；新增真实后端/安全源码的注入式测试，覆盖专家领域扫描、确定性 MoE 骨架和 LLM 综合增强路径。

## [0.1.85] - 2026-06-01

### Changed
- **MCTS Analysis Tool 拆分** — 将 `analysis_mcts` 的 Tool 包装迁移到 `analysis_tools.mcts`，保留 `analysis.py` 兼容 wrapper、可选源码目标扫描和全局 router 行为；新增真实分支源码的注入式测试，覆盖决策分支/异常路径扫描、确定性多路径剪枝报告和 LLM MCTS 深化路径。

## [0.1.84] - 2026-06-01

### Changed
- **Graph Analysis Tool 拆分** — 将 `analysis_graph` 的 Tool 包装迁移到 `analysis_tools.graph`，保留 `analysis.py` 兼容 wrapper、默认当前目录扫描和全局 router 行为；新增真实临时 Python 项目的注入式测试，覆盖 AST 实体/关系/循环依赖扫描、源码读取和 LLM 图谱推演路径。

## [0.1.83] - 2026-06-01

### Changed
- **DSPy Analysis Tool 拆分** — 将 `analysis_dspy` 的 Tool 包装迁移到 `analysis_tools.dspy`，保留 `analysis.py` 兼容 wrapper、默认当前目录扫描和全局 router 行为；新增真实 prompt 文件的注入式测试，覆盖 Prompt 模板/Few-shot/Metric 静态扫描、baseline metric 和 LLM 编译建议路径。

## [0.1.82] - 2026-06-01

### Changed
- **Page Analysis Tool 拆分** — 将 `analysis_page` 的 Tool 包装迁移到 `analysis_tools.page`，保留 `analysis.py` 兼容 wrapper 和全局 router 行为；新增注入式 router 窗口测试，覆盖真实上下文窗口裁剪、确定性分页报告和 LLM Page 增强路径。

## [0.1.81] - 2026-06-01

### Changed
- **Sleep Analysis Tool 拆分** — 将 `analysis_sleep` 的 Tool 包装迁移到 `analysis_tools.sleep`，并通过 `router_getter`、`run_analysis`、`resolve_target`、`read_sources` 注入保留旧行为；新增带真实临时文件读取的注入式测试，覆盖确定性 Sleep 报告和 LLM 增强路径。

## [0.1.80] - 2026-06-01

### Changed
- **Analysis Tool 拆分样板** — 将 `analysis_entropy` 的 Tool 包装迁移到 `analysis_tools.entropy`，保留 `analysis.py` 的兼容导出和全局 router 注入行为；新增注入式 runner 测试，后续可按同一模式逐步迁移其他 analysis 工具，降低单文件维护压力。

## [0.1.79] - 2026-06-01

### Changed
- **Subagent 生命周期工具边界加固** — 为 `delegate_task`、`spawn_agent`、`destroy_agent` 和 `list_agents` 增加工具元数据，并在委派、创建、销毁前校验 Agent 名称、任务描述、成功标准和上下文长度；`destroy_agent` 现在显式标记为需要确认的破坏性工具，避免异常名称或超长内容进入子 Agent 调度链路。

## [0.1.78] - 2026-06-01

### Changed
- **Team Protocol 工具边界加固** — 为 `team_signal` 和 `team_status` 增加状态变更/只读工具元数据，并在发布团队事件或读取状态前校验文本字段、黑板 key、布尔开关和显示数量，避免异常团队消息污染协作协议或黑板状态。

## [0.1.77] - 2026-06-01

### Changed
- **Skill 工具边界加固** — 为 `SkillTool` 和 `skill_execute` 增加工具元数据，普通 skill 标记为只读，包含动态命令注入的 skill 标记为需确认；同时校验 skill 参数长度和必填参数，避免超长参数或空必填值撑爆上下文。

## [0.1.76] - 2026-06-01

### Changed
- **Blackboard 工具边界加固** — 为 `blackboard_read` 和 `blackboard_write` 增加只读/状态变更工具元数据，并校验共享状态 key/value 的类型、空值、长度和路径越界片段，避免多 Agent 协作黑板被异常 key 或超大内容污染。

## [0.1.75] - 2026-06-01

### Changed
- **Code Execute 工具边界加固** — 将 `code_execute` 标记为需要确认的破坏性代码执行工具，并在 Docker 或本地降级执行前校验代码内容、语言白名单和超时范围，避免空代码、未知解释器或异常超时参数进入进程执行路径。

## [0.1.74] - 2026-06-01

### Changed
- **Memory 工具边界加固** — 为 `memory_store` 和 `memory_recall` 增加工具元数据、分类白名单、内容/查询长度限制和 `top_k` 边界校验，避免空记忆、异常分类或过大召回请求污染长期记忆；新增无需 ChromaDB 的工具层单测，并保留真实长期记忆定向验证。

## [0.1.73] - 2026-06-01

### Changed
- **Hot Reload 工具域发现加固** — 将 `hot_reload` 标记为需要确认的运行时状态变更工具，并把 `tools` 域从硬编码短名单改为动态发现当前工具树，覆盖 `self_modify`、`self_evolve`、`forge` 与拆分后的 `analysis_support` 模块；同时为重载目标增加格式校验，拒绝非 `naumi_agent.*` 模块名。

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
