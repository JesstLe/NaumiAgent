# 启动目录工作区绑定设计

## 状态

- 日期：2026-07-14
- 状态：已批准，待实施
- 范围：首次引导、新 Terminal UI、Textual TUI、交互式权限边界

## 目标

用户从任意目录执行 `naumi` 时，该启动目录就是本轮工作区。首次引导不再询问工作区，旧配置中保存的绝对 `workspace_root` 也不能把交互界面带回另一个项目。`bypass` 仍是完整放行模式，可以操作工作区外的任意目录。

## 方案比较

### 方案 A：运行时绑定并兼容旧配置（采用）

配置仍保留 `workspace_root`，供 API、部署和显式配置命令使用；交互式入口在加载 YAML 后调用统一的运行时绑定方法，把启动目录写入内存配置。新引导只写相对值 `.`。这个方案覆盖新旧配置，同时不破坏非交互入口。

### 方案 B：彻底删除 `workspace_root`

语义最简单，但会破坏 API、部署、Workbench 和现有配置合同，迁移面过大。

### 方案 C：只修改首次引导

改动小，但旧配置仍会加载历史绝对路径，无法解决现有用户的问题。

## 配置与运行时合同

`AppConfig.bind_runtime_workspace(launch_dir)` 负责：

1. 将 `launch_dir` 展开、绝对化并解析符号链接；
2. 记录旧 `workspace_root` 对应的绝对目录；
3. 把 `workspace_root` 改为启动目录；
4. 将 `safety.allowed_dirs` 中与旧 workspace 等价的条目替换为启动目录；
5. 保留用户显式声明的其他允许目录；
6. 去重并保证启动目录始终出现在允许目录中；
7. 返回最终工作区路径。

新 Terminal UI 的 JSONL bridge 与 Textual TUI 在构造 `AgentEngine` 前调用该方法。绑定只修改本轮内存对象，不重写 `.naumi/config.yaml`。

## 首次引导

首次引导删除工作区问题。生成配置使用：

```yaml
workspace_root: .
safety:
  allowed_dirs:
    - .
```

因此同一份用户级配置可以从不同目录启动，不携带首次安装时的绝对路径。

## 权限边界

- `moderate`、`strict`、`lockdown` 等模式继续通过 `PermissionChecker` 校验路径；相对路径以启动目录解析。
- `bypass` 保持 `PermissionChecker.check()` 的首个短路分支，在路径、命令和工具规则检查前直接允许，因此不受 workspace 与 `allowed_dirs` 限制。
- 本切片不放宽其他权限模式，也不改变危险命令规则。

## 错误与用户体验

- 启动目录不存在或无法解析时返回中文启动错误，不静默退回配置中的旧目录。
- `/pwd`、欢迎页、状态栏、Harness 与 Git 状态全部消费 `AgentEngine.workspace_root`，无需各自猜测目录。
- 显式 resume 只恢复会话，不改变本轮启动工作区。

## 验证

- onboarding 测试证明不再消费 workspace 输入，YAML 不含绝对安装目录；
- config 测试覆盖旧 workspace 替换、额外目录保留、去重和符号链接解析；
- bridge 与 Textual 启动测试证明 Engine 收到启动目录；
- permissions 定向测试证明 moderate 阻止目录外访问、bypass 允许目录外访问；
- 运行真实临时目录入口，检查 ready payload 的 `workspace_root`。

## 非目标

- 删除高级 `naumi configure --workspace`；
- 改变 API server 或部署命令的显式 workspace；
- 自动修改已有 YAML；
- 改变 bypass 之外的授权策略。

## 自审

- 没有把配置文件所在目录误当工作区；
- 新旧配置均覆盖；
- bypass 全目录能力有现有代码路径与回归测试；
- 没有扩大非交互入口的行为变化；
- 没有占位内容或依赖后续才能成立的要求。
