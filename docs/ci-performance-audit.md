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
