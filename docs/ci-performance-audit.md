# CI 性能审计

## 发现

Python CI 为规避测试之间的共享状态污染，会为每个 unit 测试文件单独启动 pytest。审计时仓库有 534 个 unit 测试文件，旧工作流在一个 runner 中串行创建 534 个 Python 进程。最近多次 `main` 构建因此持续一个小时以上，同时后续提交继续创建新的长任务。

## 调整

- 保留每个测试文件独立 pytest 进程的隔离边界。
- 依据测试源码体积做确定性的贪心均衡，分成 12 个并行 shard。
- 每个 unit 文件设置 10 分钟上限，e2e/integration 设置 20 分钟上限；发生阻塞时会显示具体文件并终止，而不是占用 runner 数小时。
- 每个 shard 独立收集 coverage，最终下载、合并并继续执行 50% 覆盖率门槛。
- 同一分支的新运行会取消旧运行，避免快速提交时堆积重复 CI。
- GitHub 官方 Action 更新到 Node 24 版本，消除 Node 20 弃用警告。

## 验证边界

分片选择器通过定向单元测试，并对真实 `tests/unit` 清单检查：每个文件恰好出现一次、分配结果确定、各 shard 源码体积接近。完整测试由更新后的 GitHub Actions 首次运行验证，本地不重复执行全仓测试。

首次分片运行在约两分钟内定位到 `/evolution` 命令补全元数据遗漏多个 approval 子命令、两个 Claude 来源校验测试硬编码 Linux 虚拟环境路径、终端断言未剥离 ANSI 样式，以及 20 份近期文档未纳入治理清单的问题；旧串行 CI 长时间没有到达这些文件。相关测试现已改为使用当前 Python 解释器和纯文本断言，补全提示与真实可用子命令同步，近期文档也已按用途分类。

跨平台进化测试夹具原先固定声明 macOS，并受开发机 `core.autocrlf` 影响，导致 Linux runner 拒绝当前 platform lane、Windows 又出现补丁摘要不一致。夹具现绑定实际测试平台，并在临时 Git 仓库关闭自动换行转换，确保同一份 Patch Manifest 在 Windows 与 Linux 使用相同字节。

分片还暴露了三类被旧串行运行长期掩盖的治理债务：Pursuit 恢复健康检查仍构造旧 schema、13 个新模块没有登记 domain ownership、60 个新增测试绕过权威 runtime composition。恢复夹具已升级到 schema v2，新模块已归属到 runtime/tools，新增测试构造已迁回 `create_agent_engine()`，并保留原有 171 个 legacy 构造上限。Agent Worker 篡改测试的冷启动握手窗口也从 2 秒调整为 10 秒，避免 runner 首次加载时把启动抖动误判成认证失败。

后续静态审计中，`ruff check src` 与 `compileall` 通过，721 个模块的 import graph 没有 import-time SCC；Git 跟踪清单中没有缓存、日志、备份或构建产物。零入边模块主要是 CLI 入口、兼容导出和动态注册面，缺少可安全删除的直接证据，因此没有仅凭引用计数删除公共模块。当前仍有一项明确架构债务：`orchestrator/engine.py` 为 435,191 bytes，超过 Harness 单文件知识索引 262,144 bytes 上限；本轮把真实仓库用例改为明确选择 `context_assembly.py`，后续应按职责拆分 Engine，而不是继续放大知识索引上限。

第二轮分片运行继续发现两处版本绑定漂移：Agent publication 恢复测试硬编码 Agent Control schema 6，而权威常量已升级为 7；Claude 语义映射仍绑定新增协议 capability 之前的 contract 摘要。测试现改为引用 `AGENT_CONTROL_SCHEMA_VERSION`，语义映射及其下游 UI state mapping 同步绑定当前 protocol contract，避免后续 schema 演进再次产生同类假失败。

同轮还发现 Harness 迁移测试把 schema 21 写死，而 Store 已演进到 26；相关断言已统一引用 `HARNESS_STORE_SCHEMA_VERSION`。Release 百分比发布夹具则把信任密钥截止时间固定为 2026 年 9 月 9 日，导致真实日期越过窗口后所有发布链路测试同时失效；共享策略夹具现按场景基准时间扩展有效窗口，同时保留固定历史构建时间的覆盖。

Windows 定向验证进一步发现 artifact 下载在 staging 文件设为只读后执行原子移动，失败清理又直接删除只读文件，最终触发 `WinError 5`。下载提交现于 Windows 在 staging 仍可写时完成 fsync，随后原子移动并将目标封存为只读；异常清理仅对确认的普通 staging 文件恢复写权限后删除。Harness 初始迁移的精确表清单也补齐 schema 26 新增的四张 runtime/retry 表。

继续追踪发现 Windows 的 `os.fsync()` 不能对 Python 以只读模式打开的句柄执行，导致 artifact 复用和 immutable slot 安装失败。新下载和 slot 内容会在仍可写时用 `r+b` 完成 fsync；对已校验且已封存的只读文件不重复执行 Windows 不支持的二次 flush，仍保留摘要、大小、普通文件与只读属性校验。

Archive Admission 的并发测试还暴露 `ReleaseSlotStore.install()` 缺少进程内互斥：多个服务线程可同时创建同一 content-addressed slot，在 Windows 上产生路径 canonical 校验漂移或冲突。Store 现以可重入锁串行化单实例安装事务，数据库与 immutable slot 收口保持原有幂等语义。

`test_engine.py` 在 Linux coverage 模式下超过统一 10 分钟文件上限，但此前已持续执行并非死锁。CI 仍对普通 unit 文件保留 10 分钟上限，仅为该已知大文件设置 20 分钟；长期修复仍是拆分超大的 Engine 与测试文件。`/evolution` 命令索引也补回 rollback execute/outcome 的明确语法，避免通配提示掩盖真实可用子命令。

命令索引模型将语法提示限制为 300 字符；补充 rollback 子命令后一度越界。提示现将四个 discover 子命令折叠为 `discover-*`，保留需要被明确发现和测试的 approval 与 rollback 子命令，总长度为 299 字符。

死代码扫描还发现长期记忆 `forget_old(max_age_days=...)` 在两阶段遗忘改造后遗失了参数接线，调用方传入自定义保留期也始终按固定 90 天执行。当前保留 `0` 代表默认策略的兼容语义，正数会覆盖进入 dormant 的天数，负数策略会在接触 ChromaDB 前明确拒绝；定向记忆测试覆盖默认策略、自定义策略、永久删除与非法输入。

同一轮扫描确认 `cli/commands_meta.py` 还保留了一套无人引用的旧 `/memory` 命令实现，并且内部没有等待异步记忆接口、使用了已不存在的 `limit` 参数与旧结果字段。权威入口实际位于 `main.py` 且已有正确异步调用，因此删除这组不可达副本，避免未来误接入后重新引入运行时错误。Chrome profile 同步也移除了一个恒为假的事件循环三元表达式，文件新鲜度现在直接由 Cookies 文件时间戳计算，并用新鲜与过期两个真实文件状态定向验证。

第三轮 Linux 分片把五类接口演进漂移同时暴露出来：Bridge 交互测试夹具缺少新增的恢复补位回调；Tool Catalog miss 测试仍试图注册已被注册表边界拒绝的非法工具名；Harness baseline 专项夹具写死了旧协议完整 capability 列表；输出保留命令直接断言 Rich ANSI 文本；New UI 已声明 Pursuit 恢复动作但共享必需能力和 Textual TUI manifest 未同步。相关测试现分别绑定当前接口或先转为纯文本，TUI manifest 使用现有 `/pursue resume` 共享执行路径作为证据，所有失败文件均已定向通过。

扩大 Harness 相邻测试时还发现一个 Windows 可执行性问题：符号链接越界、非法 JSON 与超大 fixture 原本合并在一个测试中，普通 Windows 账户因缺少 symlink 权限会在准备阶段失败，后两项边界检查随之完全不执行。测试现拆为独立用例，仅在系统明确返回 WinError 1314 时跳过符号链接场景，非法 JSON 和超大 fixture 在 Windows 仍持续验证。

本轮复扫 Git 跟踪清单仍未发现缓存、日志、备份、临时文件或构建产物；定向测试与 `compileall` 在本地生成的 53 个 `__pycache__`、pytest/ruff/mypy 缓存目录已在工作树边界内清理。Vulture 以 90% 置信度复扫只报告第三方协议要求保留的形参：prompt-toolkit completer 的 `complete_event`、context manager 的异常三元组，以及下载 stream 协议的 `chunk_size`；没有新的可安全删除实现。

性能结构债务也不只存在于 `engine.py`：`harness/store.py` 约 536 KB，`main.py` 约 390 KB，`ui/bridge.py` 约 327 KB，`tools/evolution_review.py` 约 288 KB，`daemons/agent_jobs.py` 约 257 KB。它们会增加导入、静态分析、代码索引和修改回归成本，但本轮没有把机械拆文件宣称为运行时优化；后续应先为各职责建立调用与并发基准，再逐模块迁移并保持公共接口稳定。

新 CI 在前述失败通过后继续运行，进一步发现 `test_harness_surfaces.py` 也复制了同一份旧协议完整 capability 列表，导致离线 Eval 失败并连带阻断 Baseline 晋升。该共享 surface 夹具已同样绑定 `PROTOCOL_CAPABILITIES`，Linux 分片暴露的四个失败链路由一次根因修复收口。

Windows 真实 slash 流程随后暴露了两个生产可执行性问题。Harness Sandbox 快照目录直接拼接 `manual:<session-id>`，冒号会触发 WinError 123；目录名现改为由 run、check 与 source digest 共同生成的固定长度 SHA-256 身份，原始 run id 仍保留在 manifest 和回执中。基础设施异常现在同时显示稳定的 `sandbox_unavailable` 或 `sandbox_infrastructure_error` 错误码，用户可以据此区分缺少隔离后端与其他执行故障。Surface 测试 Profile 也改用当前 `sys.executable`，避免 WindowsApps 的 `python3.exe` 别名存在但不可访问时产生 WinError 1920。Windows 定向结果为 15 passed、4 个真实隔离后端场景按设计 skipped，另有便携快照路径用例通过。

分片 11 继续向后执行后发现 `naumi run` 生命周期测试仍构造只有 `log_level` 的旧配置替身，而单任务入口已根据 `config.engine.provider` 选择 Naumi 或 Pi 引擎。夹具现明确声明 `provider=naumi`，成功与异常路径仍验证 Engine shutdown，4 个定向用例通过。

分片 8 还暴露独立 Agent Worker 的两处时间竞态测试：健康检查把已读取的旧 heartbeat 时间当作当前评估时间，活跃子进程写入下一次 heartbeat 后会被误判为 clock regression；Claim 续租则用固定 350ms 睡眠假设 coverage 下的调度时延。测试现以当前时刻加小幅容差评估健康状态，并轮询持久 `agent_job_claim_renewed` 回执直至有界超时。完整文件及 CI 同等 coverage 模式均为 10 passed。

同一分片继续到 Agent Control 时，动态 Agent 的首次 delegate 在 coverage 冷启动下超过测试硬编码的 1 秒等待窗，实际执行随后可以正常开始。等待窗调整为 10 秒并仍由 `asyncio.wait_for` 有界控制；普通与 coverage 定向运行均为 5 passed。

分片继续执行到 Unix 安装脚本时，端到端夹具在 Linux runner 上伪造 `Darwin/arm64`，并生成 `macos-arm64` 发布包；安装后的 Python Launcher 会重新读取真实主机并按设计拒绝该包，返回 `release_slot_target_mismatch`。夹具现由真实 Unix 主机计算 release target，同时让 fake `uname` 与该目标保持一致；Windows 只执行脚本静态检查并明确跳过 Unix 安装链路。修复没有绕过或削弱 host target 校验，Windows 定向结果为 6 passed、1 skipped。

下一轮分片暴露 Pursuit 恢复账本无法表达“同一内容的机械裁决再次发生”：`decision_id` 由裁决内容生成，同一个 blocked 状态在第二次恢复时会复用 identity，而单行裁决表仍保留第一次发生时间，导致 terminal outbox 将合法的恢复后终态误判成准入前旧事实。当前保持裁决内容表不可变，新增按 run、decision 与时间记录的 occurrence，并由 `PursuitRun` 明确指向本次发生时间；恢复对账、outbox 与回执读取都校验该 occurrence。原失败文件普通和 coverage 模式通过，相邻恢复、对账、终态与 outbox 链路共 75 个用例通过。

Stable Deployment Intent 的 target 漂移测试还把变化值写死为 `linux-x64`；Linux runner 的真实目标本来就是该值，因此断言的前置条件并未成立。夹具现保存实际原目标并选择一个确定不同的目标，恢复时也回写原值。Windows 因该真实 baseline 用例依赖 POSIX shebang 而按设计跳过，本地已验证当前主机上的替代目标确实不同，Linux 分片负责端到端执行。

异步性能复扫确认 `LongTermMemory` 的公开方法虽然声明为 `async`，但 ChromaDB 初始化、embedding 查询、磁盘更新、遗忘、搜索和导出都直接运行在事件循环线程；慢向量查询会连带阻塞流式输出、心跳和同会话任务。所有 ChromaDB 事务现通过 `asyncio.to_thread` 移出事件循环，并由实例级可重入锁保持初始化、去重与更新的串行一致性；去重查询同时移除一次重复 `count()`。并发慢后端测试确认事件循环保持可调度、存储调用不重叠且不在事件循环线程执行，真实 ChromaDB 的 43 个用例在普通和 coverage 模式均通过。

同类扫描还发现 Agent 高频使用的 `glob`、`grep`、`file_read`、`file_write` 和 `file_edit` 虽然提供异步 `execute()`，目录遍历、全文读取和落盘却仍同步占用事件循环；大工作区搜索或慢磁盘会暂停流式输出并拖慢并行会话。五个文件工具现保留原有同步实现与返回协议，由异步入口统一通过 `asyncio.to_thread` 调度。真实文件读写、搜索与编辑链路共 16 个定向用例通过，慢 I/O 调度用例确认五个工具运行期间事件循环仍可调度，且实际工作线程不等于事件循环线程。

聊天附件路径原先以 `await file.read()` 一次性把文件装入内存，再在 API 事件循环同步写盘；上传没有体积上限，同名文件还会覆盖旧引用，构建模型上下文时也会读取整个来源文件后才截断。当前上传按 1 MiB 分块写入临时文件，限制为 20 MiB，并在线程中 flush、fsync 后原子提交；每份引用使用独立 source id 文件名，数据库登记失败会清理孤儿文件，附件目录和跨平台文件名均在写盘前校验。上下文预览只读取最多 20,001 个字符并在线程中执行。11 个定向用例覆盖空文件、同名附件、精确上限、超限清理、目录越界、非法文件名、数据库故障、慢磁盘调度和有界预览。

后续 Linux 分片继续发现三处夹具漂移。Terminal Event Bridge 的最小 Engine 替身缺少 Sandbox 恢复快照所需的 `workspace_root`；夹具现复用事件日志 Store 的真实工作区，普通及 coverage 模式均为 5 passed。Evolution Review UI 把治理拒绝时间固定为 2026 年 7 月，却用运行时当前时间判断 cooldown，日期越过冷却期后预期从 `needs_evidence` 变为 `review_ready`；测试治理读取现固定在场景时刻，14 个用例普通及 coverage 均通过。Pursuit 恢复夹具也把后台完成回执写死在 2026 年 7 月，`BackgroundRunner` 初始化会按 7 天策略正确清理它，随后恢复自然找不到回执；夹具改用当前终态时间，文件普通及 CI 同等 coverage 模式均为 30 passed。

后台任务 watcher 原先通过 `proc.communicate()` 在内存中累计完整 stdout，进程结束后才同步写日志；长时间编译、测试或服务输出可能让 Agent 进程内存随日志无限增长，并在最终落盘时阻塞事件循环。当前 stdout 按 64 KiB 异步读取并在线程中直接写入日志，仅保留 8 KiB 原始字节用于生成 2,000 字符预览；日志完成时 flush、fsync、close，取消会等待已派发的单次文件操作安全收口，超时仍终止进程并保存已读输出。输出存储失败会终止仍存活的子进程并形成明确失败回执。后台子系统普通及 CI 同等 coverage 模式均为 36 passed，包含约 2.5 MiB 分块输出和写入故障路径。

分片 2 随后发现 runtime composition 守卫把 `main.py` 中 factory 调用次数硬编码为 3；新增合法 CLI 入口后调用变为 4，测试因此误报架构回退。守卫现通过 Python AST 检查产品入口确实调用 `create_agent_engine`，并禁止直接调用 `AgentEngine`，保留原始架构约束同时允许入口数量演进。

Workbench 右侧 Diff 面板原先为每个文件并发执行 Git，并通过 `communicate()` 把完整 patch 一次性收进内存；8 个大文件会同时放大服务内存，最终 JSON 响应也没有总量上限。Git 输出现按 64 KiB 流式读取：单文件 patch 最多 512 KiB，单次响应最多 4 MiB，Git 元数据设置独立 8 MiB 完整性上限，并禁用 external diff 与 textconv。超限文件保留路径和完整 numstat，在 Web 与 Web2 明确显示“截断”或“未加载”提示；未跟踪文件读取移出事件循环。真实仓库的大 tracked diff、九个大 untracked 文件和慢磁盘调度场景均由定向测试覆盖。

相邻功能检查还确认未跟踪文件一直被后端错误标为 `unstaged`，使 Web2 已提供的“未跟踪”筛选始终为空。后端现返回独立 `untracked` stage；旧 Web 的默认“未暂存”视图显式合并 `unstaged` 与 `untracked`，因此新文件继续可见，同时共享协议和 Web2 筛选语义一致。

审批证据收集也在解析后才截断 Git status/diff，意味着大 worktree 仍会先完整进入内存；原有 30 文件上限还有 off-by-one，实际可能返回 31 个文件，并且所有裁剪都对审核人静默。异步子进程有界读取现下沉到 runtime 公共层，审批 status 限 1 MiB、diff 限 4 MiB，禁用 pager、external diff 与 textconv；状态使用 NUL 协议正确保留空格和 Unicode 路径。200 文件、30 个 diff 文件和单文件 4000 字符的展示上限都会形成明确 warning，Textual 审批页据此显示“待补证据”，不会把不完整 diff 标成可进入人工判断。

分片 6 发现 Harness 工具证据虽然字段名为 `result_size_bytes`，却优先读取字符数 `content_length`；包含中文等多字节字符时会把 17 字节记录成 13，影响证据大小审计。Engine 已同时发布权威 `content_bytes`，Collector 现优先使用该字段，旧事件缺少字段时再从内容按 UTF-8 计算。

分片 4 的 Store Catalog 测试同时引用 `AGENT_JOB_SCHEMA_VERSION` 又硬编码旧值 6；Agent Job Store 已按迁移链升级为 7，Catalog 本身正确。断言现只验证 Catalog 与权威常量一致，避免下一次合法 schema 迁移继续产生伪失败。

分片 9 暴露 Browser TaskRunner 的终态发布顺序：任务字典先写入 `completed` 或等待态，持久 heartbeat 随后才写入 `stopped`/`waiting`，因此 UI 和调用方可以真实观察到“任务已完成但心跳仍 running”的矛盾状态。终态现先提交 heartbeat，再原子更新对外 run status 并发送 `run_finished`；等待和恢复仍在状态持久化前同步 heartbeat snapshot。

分片 7 的 Installation Daemon 端到端测试在持久 delivery 刚写入 `completed` 时立即读取 Worker 快照，偶发落在整轮统计尚未发布的合法窗口。测试现先有界等待持久终态，停止数据库轮询后再等待 `returned_count`，保持 Worker 以完整 pass 原子发布统计的语义，也避免 coverage 冷启动时短超时循环持续与后台写入争用连接；该链路依赖 POSIX executable slot，因此 Windows 按既有边界跳过，由 Linux CI 执行。相邻的交互式 Engine 夹具还缺少两个新增 Stable Promotion Worker 及其禁用配置，导致长期服务启动测试在进入目标断言前失败；夹具已补齐当前 composition contract，普通及 coverage 定向模式均通过。

工具复扫发现 `yaml_micro_verify` 固定调用 `python3`，Windows 虚拟环境通常只有 `python.exe`，启动失败会在 Ruby fallback 之前直接抛出；fallback 使用的 `YAML.load_file` 还会引入与安全加载不同的反序列化语义。`yaml_validate` 同时保留了第二套同步 YAML 解析。两个工具现复用当前进程的 `yaml.safe_load` 底层，并通过 `asyncio.to_thread` 移出事件循环；极简工具保留原结果标记，详细工具保留中文错误回执。真实有效与非法 YAML、共享解析协议及慢解析调度共 4 个定向用例在普通和 coverage 模式均通过。

运行回执的 Git 探针仍通过 `communicate()` 把完整 status 与 numstat 同时装入内存，500 路径裁剪只在完整读取后发生；大量未跟踪文件还会在事件循环线程同步读取第二遍以统计行数。探针现复用公共有界子进程读取，普通文本限 64 KiB、status 限 1 MiB、numstat 限 2 MiB，截断时只解析完整 NUL 记录并把证据不完整写入回执 warning；stderr 独立排空并只保留 4 KiB，避免管道阻塞。未跟踪文件行数统计移到工作线程。真实 Git 净变更、非仓库、强制截断和慢磁盘调度共 5 个定向用例在普通和 coverage 模式均通过。

扩大运行回执测试时发现两个 Windows 夹具边界：删除后置条件用例把未引用的绝对 Windows 路径直接拼进 POSIX lexer，反斜杠被当作转义字符，导致目标识别失败；用例现使用相对工作区路径表达真实 shell 调用。符号链接删除用例仅在系统明确返回 WinError 1314 时跳过，其他创建错误仍失败。完整文件在 Windows 为 19 passed、1 个权限受限场景 skipped。

下一轮 Linux 分片在 Installation Daemon 的并发轮询中发现 Delivery Store 的读取一致性缺口：`get()` 先读取主记录，再用第二条查询验证 append-only 事件链。后台 Worker 在查询之间提交 ACK/completed 转换时，读取方会把旧主记录与新事件链拼成不可能状态，并误报存储损坏。`get()` 现用一条 `LEFT JOIN` 在同一个 SQLite statement snapshot 中读取主记录和完整事件链，既避免撕裂读取，也不延长高频轮询的读锁；重复 enqueue 的既有记录路径也在释放 `BEGIN IMMEDIATE` 前完成验证。回归用例在单语句快照返回后强制启动并发 claim，验证读取方仍返回完整 queued snapshot、写入方随后进入 in-flight。该真实 release fixture 依赖 POSIX executable slot，Windows执行 ruff 与 compileall；Linux coverage 分片中 Installation Daemon 完整文件为 11 passed，确认此前的链损坏误报与统计等待超时均已消失。

分片 11 的 Pi Web API 测试把“接受 engine 字段”与 runner 是否真实安装 `pi` 二进制绑定，Linux 正确返回不可用 400 后被误判为 schema 回归。字段接受用例现显式注入可用性前置条件；独立的不可用用例继续使用不存在的二进制并验证 400。相邻 SessionStore 持久化用例也在 `finally` 关闭连接，避免 pytest 事件循环结束后 aiosqlite 工作线程继续回调。定向普通与 coverage 模式均为 3 passed，完整文件 16 passed，且不再产生线程泄漏 warning。

分片 10 的流式事件穷尽测试复制了一份 transport 类型表，却漏掉产品已明确声明的 `HARNESS_COMPLETION_CORRECTION -> PHASE_SUMMARY` 映射，因此把可读的完成门禁纠偏摘要误判为应保留的原始 runtime event。测试表现已与权威封闭映射同步，保留 phase summary 的结构化 `items` 数据和活动摘要；完整文件普通与 coverage 模式均为 50 passed。

运行 `34692164470` 继续暴露三个独立问题。分片 7 的 LiteLLM 回环传输测试把 Anthropic `thinking` 请求体完整固定为 `{type: adaptive}`，而当前 Linux 安装会在保持 adaptive 推理的同时增加 `display: summarized`；断言现验证稳定的 adaptive 契约，并只接受缺省或 summarized 两种已知展示值，避免把依赖版本补充的展示提示误判为传输回归。

分片 3 的会话恢复失败测试存在确定性死锁：测试替换了 `resume()` 并等待替身进入，却向 `load_session()` 传入不存在的 ID。生产代码会先由 `load()` 正常返回空值，根本不会进入 `resume()`，因此测试永久等待。夹具现先创建真实候选会话，再让 `resume()` 返回空值，准确覆盖恢复第二阶段失败后的 transition fence 与权限授权保留；普通及 coverage 定向模式均为 1 passed。

分片 0 的两个 Agent 停止 Bridge 用例仍假设默认委派走嵌入式 `agent.execute()`，但当前生产组合已携带模型配置并启用独立 Agent Worker。用例替换的嵌入式执行函数因此不会被调用，短等待失败后又在清理未进入目标状态的委派时阻塞。两个场景现明确选择其要验证的 embedded backend，并将冷启动及停止收口等待调整为 10 秒有界窗口；普通及 coverage 定向模式均为 2 passed。该修复没有关闭产品默认的独立 Worker，只消除了测试对默认组合演进的隐式依赖。

同一执行中的 `test_engine.py` 在前述死锁修复后仅用 78.52 秒完成全部 155 个用例，证明原 20 分钟失败来自夹具等待而非文件规模本身。随后暴露的三个 Todo 联动用例也只替换了嵌入式 `coder.execute()`，默认独立 Worker 因而绕过替身并尝试真实模型传输。三个场景现与 Bridge 停止用例一样明确选择 embedded backend，继续验证成功、业务失败和执行器异常对 Todo 状态及事件流的映射；普通及 coverage 定向模式均为 3 passed。

分片 5 的共享终端 golden 已新增 deterministic capture 元数据，但 parity loader 的封闭顶层键集合仍停留在 capture 加入前；同一份 tool result golden 也缺少当前消息协议中的 `error_code` 与 `retryable` 字段。Fixture 现完整表达 capture 与工具错误恢复契约，终端引擎适配、Textual 渲染和 Ctrl+C 生命周期文件在普通及 coverage 模式均为 5 passed。

补齐 golden 后，本地普通模式还复现了 TUI 状态竞争：启动时异步执行的会话协调恢复可以在用户取消当前运行之后完成，并覆盖“已取消当前运行”的即时反馈。恢复任务现捕获启动时状态，只在状态栏期间没有被后续用户动作或运行事件修改时发布摘要或失败提示；确定性并发用例与终端生命周期文件在普通及 coverage 模式均为 6 passed。

运行 `34696672524` 的分片 0 还暴露 Bridge 产品身份测试把协议兼容摘要写死为“只有当前摘要”，而权威 Registry 已保留 12 个历史兼容摘要供旧客户端协商。测试现直接绑定 `load_protocol_event_registry()` 返回的当前摘要与完整兼容清单，避免协议演进后把正确的兼容窗口误判为回归。

同一分片的真实 Engine 流式工具生命周期用例替换了 `execute_tool()`，因此不会实际写入文件，却仍以“写入文件”声明动作型任务。新增的工作区变更完成门禁会正确重试并拒绝这种伪完成，最终回答自然不会发布。用例现明确声明只演示工具生命周期且不创建或修改文件，继续覆盖 `tool_prepare`、`tool_use`、`tool_result` 与最终 token 的真实 Bridge 事件顺序；两个分片 0 根因在普通及 coverage 模式均为 2 passed。

分片 9 的 Installation Daemon 用例确认持久 delivery 已进入 `completed`，但仍用独立的 3 秒循环等待 Worker 在整轮末尾发布内存统计；Linux coverage 下完整 pass 偶尔超过该窗口。用例现通过 Worker 的 `run_once()` 互斥锁等待正在执行的 pass 原子收口，再读取 `returned_count`，既不延长任意睡眠，也不会与后台 pass 并发修改统计。Windows 完整文件为 6 passed、5 个 POSIX executable 场景按既有边界 skipped；该 mTLS 场景由下一轮 Linux CI 验证。

后续异步子进程复扫发现 `code_execute` 虽然最终只显示 100 KiB 输出，却先通过 `communicate()` 将 stdout 与 stderr 全量读入内存，再做字符串截断。失控代码在最长 60 秒窗口内可能持续输出并显著放大 Agent 内存。Docker 与本地降级执行现并发排空两个管道，每路只保留 100 KiB 预览，同时累计真实字节数用于明确截断提示；超时或调用任务取消都会终止并等待子进程退出后再收口 reader。Docker 可用性探针超时也不再遗留后台进程。真实本地 Python 的正常、异常、超时、空输出、双管道各 300 KiB 输出及探针超时场景共 27 个用例通过；首次 26 用例 coverage 运行通过，新增探针回收用例由下一轮 CI 覆盖。

运行 `34699094393` 的分片 5 随后暴露 Independent Review Store 的首次连接竞态：多个执行器同时打开同一 SQLite 文件时，每个业务方法都会重复执行 schema 初始化并切换 `journal_mode=WAL`，其中一个连接持有初始化锁时，另一个连接会直接收到 `database is locked`。Store 现将 schema 初始化从业务事务中分离，同一实例只执行一次，并为所有连接设置 10 秒 `busy_timeout`；多个 Store 实例同时首次初始化时，WAL 切换对锁冲突进行最长 10 秒的有界重试。真实 SQLite 回归分别覆盖同一 Store 8 路首次 claim 和 8 个 Store 实例同时初始化同一数据库，coverage 模式为 2 passed。

生命周期 Shell Hook 也会通过 `communicate()` 全量缓存外部命令的 stdout 与 stderr；用户配置的 Hook 在超时窗口内持续输出时，可以直接放大主 Agent 内存，调用任务被取消时也没有进入既有的超时回收路径。Hook 现并发排空三个标准流，stdout 只保留前 64 KiB 以解析首行控制 JSON，stderr 只保留末尾 16 KiB 供故障日志使用，并统计真实字节数形成截断告警；超时、取消和标准流异常都会终止并等待整个进程树。真实 300 KiB 双管道输出与运行中取消场景均通过，Shell Hook 文件在 coverage 模式为 13 passed。
