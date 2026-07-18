# EVO-02.6b Postflight Diff/API Guard v1

## 目标

在隔离 worktree 的实际字节写入后、Patch Journal 提交前，重新从精确 Git baseline 与磁盘现状构建
完整 diff 和公共 API 事实。任何范围、摘要、行数、文件模式或既有公共 API 漂移都会使本轮写入整体
回滚；交互权限和 bypass 均不能跳过该门禁。

本切片不判断行为语义是否正确，也不把 `postflight_passed` 等同于可推广。回执始终保持
`execution_ready=false`，后续仍须经过 EVO-02.7、HAR-08 和 EVO-03。

## 权威输入与重建原则

`EvolutionPostflightGuard.inspect()` 同时绑定：

- Experiment Contract 与其中的精确 baseline commit；
- active Worktree Lease；
- Source Snapshot；
- Mutation Plan 的完整排序文件集；
- 写前 Static Guard Receipt；
- Patch Writer 已经写入隔离 worktree 的真实字节和文件元数据。

Postflight 不信任调用方提供的 before 内容或预先计算的 API 结论。修改文件通过 `git show
<commit>:<path>` 读取 baseline blob，通过 `git ls-tree` 读取 baseline mode；创建文件则证明 baseline
中不存在该路径。磁盘侧只接受普通文件，不跟随 symlink。

## 完整 Diff 门禁

每个 `PostflightDiffFact` 机械记录并复核：

- 路径、文件类型和 create/modify 操作；
- baseline/写后 SHA-256；
- canonical unified diff SHA-256，但不保存 diff 正文；
- added/deleted line 数；
- baseline/写后 POSIX mode；
- 公共 API 前后摘要、符号数量及 `unchanged/additive/not_applicable` 结论。

文件集合必须与 Mutation Plan 完全相等且按稳定顺序覆盖。修改文件不得改变 0644/0755 模式，新文件
必须是 0644。摘要或行数必须与 Static Guard Receipt 一致；文件、baseline 或 unified diff 超过安全
上限时 fail-closed。

## Python 公共 API 指纹

Python 使用 `ast` 结构化解析，不使用正则近似。指纹覆盖：

- sync/async function、位置参数、关键字参数、可变参数、注解和默认值；
- 返回类型、decorator 与 type parameters；
- public class 的基类、关键字、decorator、public method 和 class value；
- module public assignment、annotation、import/re-export；
- literal `__all__`；每个导出项独立建模，因此新增导出是 additive，删除或改变既有导出是 breaking。

函数体和 private symbol 不进入公共 API 指纹。既有符号缺失或签名变化触发
`postflight_breaking_api` 并回滚；只增加符号允许通过。语法错误或非 UTF-8 输入 fail-closed。

## 语言支持边界

- Python：结构化 AST API guard；
- Markdown/YAML/JSON/TOML/普通文本：API 检查明确为 `not_applicable`，仍执行完整 diff 门禁；
- JavaScript/TypeScript/Go/Rust/Swift 等源码：当前没有随运行时交付的可靠 parser，返回
  `postflight_api_unsupported`，不以正则或文件扩展名假装完成语义审查。

后续为新语言引入 parser 时，必须版本化 policy、给出公开符号兼容规则和真实边界用例，不能静默降级。

## Writer 与回滚集成

单文件和多文件 Writer 都在完成原子 replace、旧的摘要/Git scope postflight 之后运行本 Guard，并在
Guard 通过后才把 journal 标记为 committed：

- 单文件失败恢复 baseline 字节与 mode；
- 多文件任一项失败，严格逆序恢复整个 write-set；
- typed Postflight error code 保留到失败回执/journal；
- committed replay 会从当前磁盘重新计算 Postflight Receipt 并要求完全一致。

Writer Receipt 升级为 schema v2，内嵌防篡改 Postflight Receipt、ID 和摘要。历史 v1 Receipt 与锁文件
仍可读取和恢复，避免升级后遗留 journal 无法清理；v1 不会被伪装成已经通过新门禁。

## 安全与隐私

- Receipt 不保存源代码、diff 正文、函数名或 secret；只保存排序事实与摘要；
- facts、总数、总行数、嵌套 Guard identity 和最终 Receipt identity 都会重新验签式复算；
- policy 独立于 default/moderate/bypass 等权限模式；
- Postflight 失败发生在隔离 worktree，主工作树和 index 不被修改。

## 验收证据

- 真实 Git baseline 上修改 Python 函数体并新增公共函数，通过且标记 `additive`；
- 改变既有函数参数触发 typed breaking API，文件与 Git status 回到 baseline；
- literal `__all__` 隐藏私有实现变化，新增导出为 additive，默认值和已导出 class value 变化被检测；
- TypeScript 新文件因缺少可靠 parser fail-closed，并删除已写文件；
- 写后 chmod 漂移被拒绝并恢复原 mode；
- 两文件 write-set 中一项 breaking 时全组回滚；
- v2 Receipt 嵌套篡改被拒绝，历史 v1 Receipt 仍可解析；
- 并发 replay、单文件和多文件崩溃恢复聚焦回归通过。

## 当前不足与下一步

- 尚未支持 Python 之外源码语言的结构化公共 API parser；
- Python 动态导出、运行时 monkey patch 和动态构造 `__all__` 无法由静态 AST 完整证明，遇到非 literal
  `__all__` 时不会把它误当成权威导出清单；
- 本门禁不运行测试、不比较行为指标，也不证明修改目标已经达成；
- EVO-02.7a 已把 rationale、attempt、治理 tool evidence 与本 Postflight Receipt 汇总为不可变实验
  artifact；EVO-02.7b1 已生成 proposed contents 的不可变原始 tool-call Trace，2.7b2 继续把同 attempt
  Trace 强制绑定到 Static Guard、Writer 与 Mutation Receipt v2。
