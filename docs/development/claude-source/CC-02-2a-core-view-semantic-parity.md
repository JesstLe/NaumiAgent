# CC-02.2a Ink 五类核心视图语义 Parity

## 目标

把 CC-02.1a 的可执行 Ink adapter 从单行时间线原型推进到文档要求的五类代表视图：conversation、
tool、permission、footer 与 task。验收对象是两种 renderer 对同一协议事实保留相同用户语义，不要求
ANSI 画面逐字符相同，也不改变默认 New UI。

## 共享权威

- conversation/tool 使用 UI-17.2f 的 `terminal-run-lifecycle-golden.json`，经 production
  `createInitialState()` 和 `reduceServerEvent()` 建立状态。
- permission/footer/task 使用 `tests/fixtures/cc02/ink-core-views-golden.json`；每条记录先经过
  `normalizeServerRecord()`，再进入同一个 production reducer，禁止测试直接拼 renderer 私有 state。
- current renderer 与 Ink renderer 使用同一 state、100×24 viewport 和相同 anchors。permission fixture
  仅含脱敏 `arguments_summary`，没有 API key、原始授权头或可执行私有数据。

## 实现

- Ink timeline 从“每条 message 固定一行”改为有界 presentation rows：普通对话/工具仍保持单行，
  permission 与 task 使用多行结构。
- permission 显示状态、工具名、原因和 allow/deny/session/bypass 操作提示；字段来自嵌套的 production
  permission message，不读取原始 arguments。
- task 从 production `taskPanel.items` 显示选中态、稳定 task id、status 和 label；空集合明确显示
  “暂无任务”。超大 scroll offset 钳制到最早可见内容，不再把空 frame 当成 deep-scroll 成功。
- footer 展示 mode、运行状态、权威 model identity 与实验 renderer identity；composer 保持独立一行。

## 语义验收

- conversation/tool：`执行定向验证`、`bash_run`、`定向验证通过` 在 current/Ink 共享 lifecycle frame
  中均存在。
- permission：`需要确认`、`bash_run`、原因、`全权限` 与 model identity 两端均存在；deny 终态在两端
  都显示 `已拒绝 / 结果: 拒绝`，不泄漏英文内部状态作为唯一语义。
- task：`tasks`、`编写页面`、`npm test` 与 model identity 两端均存在；空集合均明确显示“暂无任务”。
- Ink 输出行数固定、每行可见宽度等于请求宽度，重复 capture digest 稳定，render 不修改输入 state。

## 更新后的同机 release 证据

Darwin arm64 / Node v24.12.0，10,000 条消息、1,000 个工具、10MB 分页输出、120×40、3 次采样、
相同 fixture digest 与 protocol registry digest：

| 场景 | current P95 | Ink P95 | 结论 |
|---|---:|---:|---|
| tail | 0.863ms | 22.058ms | Ink 约慢 25.6 倍 |
| deep_scroll | 92.746ms | 17.207ms | Ink 约快 81.4%，但仍需 row index 证明可扩展性 |
| paged_output | 0.355ms | 18.737ms | Ink 约慢 52.8 倍 |

- [current CC-02.2a JSON](../cli-ui/evidence/CC-02-2a-current-release-darwin-arm64.json)
- [Ink CC-02.2a JSON](../cli-ui/evidence/CC-02-2a-ink-release-darwin-arm64.json)

加入多行语义后，Ink 会先把全部 message 投影为 presentation rows，再截取 viewport；这让数据比
CC-02.1a 的空边界路径更可信，也暴露出 tail/paged 的真实 O(n) 投影成本。结论继续是 `defer`。

## 未完成

CC-02.2 仍为 partial：

- Markdown、diff、fold、图片/Artifact、完整 completion card 和所有专页尚未达到 parity；
- permission/task 目前只读展示，尚未接入 Ink key handler、焦点、选择、取消和刷新；
- 还没有行高索引、overscan 或增量 reconciliation，不能把 deep-scroll 单项优势当成虚拟化完成；
- 没有真实 PTY、IME、resize、Windows/Linux、可访问性与安装体积证据；
- `renderToString()` 仍是确定性实验 frame，不是默认交互 runtime。

下一切片应进入 CC-02.3 的输入/焦点优先级，或先复用 UI-15.2 行高索引消除 O(n) 投影；在这两项之前
不允许 adopt。
