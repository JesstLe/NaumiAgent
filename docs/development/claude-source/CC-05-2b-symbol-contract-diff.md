# CC-05.2b Mapped Symbol Contract Diff

## 目标

在 CC-05.1 已批准 baseline 与 CC-05.2a Git tree diff 之上，确定性比较 mapped TypeScript/JavaScript
文件的公开导出、组件、事件和键位符号。该能力只产生内容寻址回执，不 fetch、不修改 source、不更新
mapping，也不把符号变化自动解释为应当采纳。

它解决“文件发生变化，但到底是内部实现调整还是用户可见接口变化”的证据缺口，为 CC-03 行为迁入、
CC-05.3 fixture 行为比较和 CC-05.4 impact routing 提供最小前置。

## Authority 链

`build_source_symbol_diff()` 必须依次重读：

1. CC-01.1b `SourceRefreshStore` 中最新已批准 history；
2. CC-05.1 `SourceBaselineObservation`；
3. CC-05.2a `SourceStructuralDiff`；
4. approved manifest 绑定且 SHA-256 未漂移的 legacy source map；
5. approved Git commit blob 与当前 commit/worktree 的 mapped source bytes。

回执绑定 `baseline_entry_id`、`observation_id`、`structural_diff_id`、baseline/current commit 和
`includes_worktree`。mapping 为 stale/unavailable、structural diff invalid、approved blob 缺失或输入超过
安全上限时返回 `invalid`，不得扫描任意未批准目录补齐结果。

默认平台治理库不存在已批准 history 时，命令必须以中文错误失败关闭；监控器无权自行 bootstrap 或批准
baseline。真实数据验收可使用生命周期限定的临时 Store，但不得写入用户生产治理库。

## 符号分类

共享 lexer 忽略注释，区分字符串、模板字符串与 JavaScript 正则字面量，并从 `.js/.jsx/.mjs/.cjs/.ts/.tsx`
mapped 文件提取：

- `export`：named declaration、named export 与 named default export；
- `component`：公开 PascalCase function/class/variable component；
- `event`：公开 `*Event/*Action/*MessageType` 合同，以及 `emit/dispatch/send("namespace/value")`
  的稳定事件名；
- `keybinding`：keybinding/bindings/shortcut 文件中的规范组合键或特殊键，以及公开 Binding/Shortcut
  合同。

函数/组件只比较声明签名，不把 body 实现变化伪装成公共 API 变化；type/interface/enum 与常量值保留
足以发现合同变化的有界 token 签名。Git 精确认定的 rename 会把相同签名报告为 `moved`，而不是一组
无关联的 removed/added。

变化类型为 `added|removed|modified|moved`。每项携带符号类型、名称、mapped area、旧/新相对路径和
两侧签名摘要，不保存源码正文。删除 export/component、事件变化和键位变化分别产生稳定 risk flag；
颜色与采纳建议留给后续报告层。

## 安全与有界性

- mapping 最大 4 MiB、mapped path 最多 512；
- 每个 source blob 最大 2 MiB、每文件最多 2000 个符号；
- 总 baseline/current 符号与变化各最多 10000；
- Git blob 只从本地 object database 读取，单次命令 20 秒超时；
- path 必须是规范相对路径，resolve 后不能越过 source/project root；
- receipt 采用 canonical JSON + SHA-256，字段额外项、乱序集合、计数漂移和摘要篡改均拒绝；
- receipt 不包含源码、注释、字符串正文全集、绝对路径、remote、凭据或审批人叙事。

## 人工入口

```bash
python -m naumi_agent.claude_source.symbol_diff \
  --manifest frontend/terminal-ui/cc-source-map.v2.json \
  --source /Users/lv/Workspace/claude-code \
  --project-root /Users/lv/Workspace/NaumiAgent \
  --store "/path/to/approved/claude-source.db"
```

`unchanged` 返回 0；`change_detected` 或 typed `invalid` 返回 1。该入口与 CC-05.2a 一样是维护者治理命令，
不是 Runtime 自动 fetch、source approval 或代码采纳 Tool。

## 验收证据

- 不变 baseline 连续运行产生完全相同的 receipt，Store mtime 不变化；
- dirty worktree 中 function signature、组件、事件名和组合键变化分别形成 typed change；
- Git rename 中同名同签名 export/component 形成 `moved`；
- mapping 漂移、未终止字符串、超限与缺失 approved blob 均失败关闭；
- 修改 receipt 计数或事实字段后摘要校验拒绝；
- CLI 使用隔离 Store 输出与 Python authority 相同的 receipt；
- 真实 `/Users/lv/Workspace/claude-code` 56 个 baseline/current mapped 文件、237 个符号连续扫描两次
  均为 deterministic `unchanged`，约 3.0 秒完成，临时 Store 在 bootstrap 后保持只读，source 与 mapping
  未写入；
- 只运行 CC source symbol/structural/baseline 小模块测试与 Ruff，不运行全量测试。

## 自我审视与保留边界

- 这是保守词法/声明扫描器，不是 TypeScript type checker；动态 computed export、运行时生成的事件名和
  跨文件类型解析不在本切片中。无法形成稳定符号时宁可不推断行为变化。
- `mapped_symbol_scope_incomplete` 只提示存在结构变化但符号证据未覆盖，不等价于回归。
- 本切片不消费 CC-03.1a behavior inventory，不执行交互 fixture，也不关联 Naumi owner/test；这些分别属于
  CC-05.3 与 CC-05.4。
- 不产生 adopt/defer/ignore/security_review 决策，不更新 source map，不复制或改写 Claude source。
  CC-05 继续保持 `partial`。
