# ARC-01.4b1 RuntimePaths 规范路径快照

## 为什么先做这一小步

ARC-02 Runtime Service 与 ARC-04 隔离 worker 都需要稳定、跨平台且不依赖进程当前目录的路径合同。
在本切片之前，Composition Root 与 `AgentEngine` 分别解析 workspace、session data、worktree，Engine
还直接调用 Harness 用户状态路径函数。对象图虽然已有 Port bundle，路径事实仍会重复计算和漂移。

本切片只建立 ARC-01.4b 的路径前置，不一次迁移 Harness、ChatRun、Browser、Task、Goal 等全部
Resource，不引入生命周期管理，也不开始 ARC-02/04。

## 契约

`RuntimePaths` 是 frozen/slots dataclass，字段全部为 absolute `Path`：

- `workspace_root`：启动时绑定的真实工作区；
- `runtime_data_dir`：session DB 的父目录；
- `worktree_storage_dir`：Runtime 管理的 worktree 根；
- `harness_db_path`、`harness_trust_db_path`：工作区之外的用户状态数据库；
- `browser_data_dir`、`browser_daemon_log_dir`：浏览器持久状态和 daemon 日志目录。

worktree/browser 三个 Runtime-owned 目录必须位于 `runtime_data_dir` 内。构造只解析路径，不创建目录、
数据库或后台任务。bypass 只影响 PermissionPort 的授权决策，不改变 `workspace_root` 或路径边界。

## 权威装配顺序

`create_agent_engine()` 固定执行：

1. `build_runtime_paths(config)`，只调用一次；
2. `build_runtime_ports(config, paths=paths, overrides=...)`；
3. `AgentEngine(config, ports=ports, paths=paths)`。

PermissionChecker 的 workspace/worktree allowlist 与 Engine 的 Harness/Browser/Workbench 资源现在消费
同一个路径对象。legacy `AgentEngine(config, ...)` 仍保留，但会委托 Composition Root builder 生成同一
合同；这是 ARC-01.5 前的有预算兼容入口。

## 安全与错误语义

- 相对路径、字符串冒充 Path 或 Runtime-owned 目录逃逸会在创建默认资源前失败；
- `NAUMI_STATE_HOME` 只在 Composition Root 解析，Harness DB 不落入 Agent 可写工作区；
- resolver 不调用 `mkdir`，因此验证或预览不会制造用户目录；
- 显式传入 `RuntimePaths` 时保持对象 identity，不重新解析环境或 cwd；
- Engine 不再直接导入 Harness 路径 resolver。

## 验收证据

- 单元测试验证七个绝对路径、用户状态目录未被创建、relative/escape 拒绝；
- Composition 测试验证 Paths 只构建一次，同一对象传给 Port builder 与 Engine；
- AST gate 验证生产 `AgentEngine` 构造同时显式传 `ports` 与 `paths`，纯路径合同不导入 config/adapter；
- 真实小场景从 Composition Root 创建 Engine，读取文件、执行流式工具、持久化 session/receipt，
  并确认 Engine 消费同一 RuntimePaths；
- 只运行 Runtime Composition、架构 gate 与真实 streaming 小模块，不运行全量测试。

## 未完成

- ARC-01.4b2：把 Store/Runner/Browser 等 Resource 实例和 override 迁入 typed `RuntimeResources`；
- ARC-01.4c：把 Harness/Workbench/Planner 等长期 Service 与 Tool bootstrap 移出 Engine；
- ARC-01.4d：反向、幂等、失败隔离的 `RuntimeLifecycle`；
- ARC-01.5/1.6：删除 legacy 默认入口并启用新增 import violation gate。

因此 ARC-01 和 ARC-01.4 仍是 partial；本切片只为 ARC-02/04 提供可信的路径前置。
