# CC-04.1a Skill 来源发现权威

## 目标

把现有 Skill Loader 的隐式“搜索目录顺序 + 首个同名项获胜”规则升级为可检查的 typed authority，
让用户和 Agent 都能知道 Skill 从哪里发现、哪一个真正生效、哪些被遮蔽、哪些清单无效。

本切片是 CC-04 Discovery model 的最小前置，只覆盖已经真实存在的 Skill 加载链；不虚构 Plugin
安装器，也不把合并后的 MCP 配置反推为可靠来源。

## 权威与优先级

`build_skill_sources()` 构造真实加载顺序，`SkillLoader.load_all()` 在选择 Skill 的同一次扫描中形成
`SkillDiscoverySnapshot`：

1. `workspace`：`<workspace>/.naumi/skills`；
2. `user`：`~/.naumi/skills`；
3. `configured`：`skills.search_paths` 的声明顺序。

路径先展开用户目录并规范化，再按规范路径去重；相对配置路径固定相对于实际 workspace，重复声明不导致
重复扫描。目录可用性在实际加载瞬间冻结，不在渲染时重新探测；不存在的来源仍保留在 snapshot 中，
因此用户能区分“没有配置”和“已经声明但目录不存在”。当前没有独立 system Skill 根，
所以报告不会伪造 system 来源。

同名 Skill 严格由更低 `priority` 数值胜出。候选只允许三种状态：

- `selected`：真实注册到 Skill Loader；
- `shadowed`：记录候选 manifest 与胜出 manifest，绝不静默覆盖；
- `invalid`：只公开稳定的 `invalid_manifest/read_failed` 码，不把解析异常正文带入 UI。

重复 `load_all()` 会清空旧注册和旧 snapshot，避免已删除 Skill 在热重载后继续幽灵生效。

## 双通道与终端表面

- 用户入口：`/extensions skills`，New UI 与 Textual TUI 均通过共享 slash router 执行；CLI help、补全与
  New UI command registry 同步登记。
- Agent Tool：`extension_discovery(kind="skills")`。
- 两个入口调用同一个 `execute_extension_discovery()`，读取实际 Loader 的 snapshot，不重新扫描或维护
  第二份状态。

报告使用中文展示来源优先级、生效项、遮蔽关系、无效项和安全边界。该操作只读、并发安全，不读取
Skill 正文到输出，不执行动态上下文命令，不联网、不安装、不启用、不授予权限。workspace 来源明确标记
“尚待接入 workspace 信任门”；发现不等于已信任或已阻断。

## 验收证据

- workspace/user 同名 fixture 只加载 workspace 项，并精确记录 user 候选及胜出 manifest；
- 无效 manifest 被隔离并返回稳定错误码，其他 Skill 继续加载；
- 缺失来源可见、规范路径重复项被去重；
- 删除 manifest 后再次加载不会保留旧 Skill 或旧候选；
- Slash、Agent Tool 和 renderer 返回相同内容；New UI command registry 暴露命令；
- 真实 NaumiAgent 工作区扫描只读取本地 manifest，报告当前来源与选择结果；
- 只运行 extension/skills/completer 小模块测试与 Ruff，不运行全量测试。

## 自我审视与保留边界

- 当前只完成 CC-04.1 的 Skill 子域。Plugin 尚无 manifest/安装 authority，MCP 的 Pydantic 合并配置也
  尚未保留来源 provenance；二者必须分别实现，不能用路径猜测冒充完成。
- snapshot 是当前进程加载时事实，不是持久历史；热重载会产生新 snapshot，但本切片不提供跨启动 diff。
- workspace 目前只标记为“需要信任门”，尚未阻断 Loader 注册；真正的 trust/install gate 属于
  CC-04.3，必须与 PermissionChecker 对齐后才能改变执行暴露。
- 本切片没有专用全屏扩展页、启停、卸载或升级提示；这些属于 CC-04.5。
- 因此 CC-04 只能从 `planned` 更新为 `partial (4.1a)`，不得标记 implemented。
