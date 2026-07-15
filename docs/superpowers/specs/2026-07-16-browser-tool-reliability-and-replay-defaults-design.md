# 浏览器工具可靠性与回放默认关闭设计

## 目标

修复 `browser_waitFor` 与 `browser_evaluate` 在真实 Playwright 页面中的确定性故障，
并让浏览器 trace、managed 模式视频和 attached 模式 screencast 默认全部关闭。
回放能力保留为显式配置项，只有用户主动开启时才写入相关磁盘文件。

## 已确认的根因

### `browser_waitFor`

当前 Python Playwright 的 `Page.wait_for_function()` 只允许通过关键字参数传入
`arg`。运行时仍把等待文本作为第二个位置参数传入，因此真实调用立即产生参数错误，
并被错误地包装成超时结果。

### `browser_evaluate`

运行时把用户表达式拼入 `(() => { ... })()` 函数体。`document.title` 一类表达式
没有显式 `return`，所以 JavaScript 正常执行但结果是 `undefined`，最终向 Agent 返回空字符串。
上游 browser-debugging-daemon 的正确行为是直接把原始表达式交给 Playwright。

### 自动回放写盘

managed 和 attached 两条浏览器启动路径都会无条件启动 tracing。managed 模式还总是
配置 `record_video_dir`，attached 模式总是启动 CDP screencast。浏览器停止时 tracing
必然被压缩为 ZIP，因此即使用户不需要调试回放，也会持续产生 trace 和视频文件。

## 设计

### 1. 等待条件调用

`BrowserRuntime.wait_for()` 保留现有等待语义、超时边界和返回结构，只把文本参数改为
`arg=text` 或 `arg=text_gone`。文本出现、文本消失和 CSS 选择器三条路径继续共用同一
结果契约。真实超时仍返回 `timedOut=true`；Playwright 参数错误不再被误报为超时。

### 2. JavaScript 求值

`BrowserRuntime.evaluate()` 直接调用 `page.evaluate(expression)`，与 Playwright 及上游
实现保持一致。表达式结果继续按当前规则序列化并限制为 8 KiB；对象和数组格式化为
JSON，`null`/`undefined` 返回空字符串，JavaScript 异常继续通过 `isError=true` 暴露。

### 3. 回放录制配置

在 `BrowserAutomationConfig` 中增加：

```yaml
browser:
  replay_recording_enabled: false
```

默认值为 `false`，并自动支持 Pydantic 的嵌套环境变量
`NAUMI_BROWSER__REPLAY_RECORDING_ENABLED=true`。`AgentEngine` 创建共享
`BrowserRuntime` 时传入该值；`TaskRunner` 创建并发隔离运行时时继承共享运行时的值。
其他直接创建 `BrowserRuntime` 的调用方使用构造函数默认值 `false`，不会意外恢复写盘。

当配置为 `false`：

- managed 模式不传入 `record_video_dir` 和 `record_video_size`；
- managed 与 attached 模式都不调用 `context.tracing.start()`；
- attached 模式不启动 CDP screencast 和帧目录；
- `trace_active` 保持 `false`，停止流程不会创建 trace ZIP；
- 调试状态中的 `videos` 和 `traces` 能力准确显示为不可用；
- 事件日志记录回放已禁用，但不创建回放 artifact。

当配置为 `true`，保留当前 trace、managed 视频和 attached screencast 行为，便于用户
在明确需要故障诊断时临时开启。

### 4. 生命周期与并发传播

回放开关属于 `BrowserRuntime` 实例的不可变启动策略，不在单次工具调用中隐式修改。
共享运行时、TaskRunner 隔离运行时和安全审计运行时默认一致关闭。停止流程仍保持现有
幂等、超时和 best-effort 清理语义；禁用回放只会跳过未启动的 recorder，不改变浏览器、
storage state、网络记录器或 Playwright driver 的清理顺序。

### 5. 工具文案

`browser_stop` 不再承诺一定保存 trace，而是说明它会停止浏览器并完成已启用 artifact
的收尾，避免默认关闭回放后向 Agent 提供错误预期。

## 错误与边界情况

- `wait_for` 的 `timeout` 继续限制在 1,000 至 300,000 毫秒。
- 同时提供多个等待条件时保持现有顺序语义，不在本次缺陷修复中扩展为竞速等待。
- `evaluate` 的语法错误、页面关闭和序列化失败继续返回结构化错误，不吞掉异常。
- 回放关闭时重复 `stop()` 不产生 trace，也不访问未启动的 tracing/screencast 对象。
- 回放开启但 tracing 启动失败时，浏览器任务仍可继续，现有 unavailable 事件保持有效。
- 并发任务不得因为隔离 runtime 的构造而回退到默认开启回放。

## 验证策略

每个缺陷独立走 TDD 红绿循环并单独提交。

### `browser_waitFor`

- 单元测试断言 `wait_for_function` 使用 `arg=` 关键字调用。
- 覆盖文本出现、文本消失、选择器、无条件输入和真实超时。
- 真实 Chromium 页面验证页面文字出现后返回 `matched=text`。

### `browser_evaluate`

- 单元测试覆盖字符串、数字、对象、空结果和 JavaScript 异常。
- 真实 Chromium 页面执行 `document.title`，验证标题直接返回给工具调用方。

### 回放默认关闭

- 配置测试验证默认值、YAML 显式开启和嵌套环境变量开启。
- runtime 单元测试验证默认不启动 tracing、不配置视频、不启动 attached screencast。
- 开启回放的兼容测试验证原有 trace 与视频路径仍可工作。
- TaskRunner 测试验证并发隔离 runtime 继承配置。
- 真实 Chromium 会话完成导航、等待、求值和停止后，确认 artifacts 下不存在 `.zip`
  与 `.webm` 文件；显式开启的独立场景确认两类文件仍能生成。

每个提交前运行 `ruff check src/`、import smoke 和相关 pytest；最终运行
`pytest tests/ -x` 以及完整真实浏览器闭环。

## 非目标

- 不改变外部网站的页面结构、跳转行为或百度产品形态。
- 不调整上下文 compaction 与大工具输出归档策略。
- 不删除用户磁盘上已有的 trace 或视频文件。
- 不重构浏览器工具命名、权限模型或 artifact 目录结构。
