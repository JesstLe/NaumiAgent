# 终端并行会话

## 用户入口

NaumiAgent 终端现在支持同时运行最多 10 个独立会话：

```powershell
naumi parallel --count 10 --workspace E:\Workspace\MyProject
```

在已经打开的终端 UI 中也可以执行：

```text
/parallel 10 "E:\Workspace\MyProject"
```

省略数量时默认打开 1 个新窗口，省略目录时沿用当前工作目录。每个窗口创建独立
Python Bridge、`AgentEngine`、运行任务和会话 ID，因此一个窗口推理时，其他窗口仍可继续
发送消息、切换页面或退出。

`/new` 用于当前空闲窗口内切换到新会话。当前窗口仍在执行任务时，`/new` 会拒绝切换并
提示使用 `/parallel 1`，避免运行中的工具、权限请求和完成回执被绑定到错误会话。

## 并发数据边界

- Session SQLite 使用 WAL、10 秒 busy timeout，并把 schema 升级放入
  `BEGIN IMMEDIATE`，允许多个 Bridge 共享同一配置目录和会话库。
- `.naumi/terminal-ui-state.json` 使用跨进程锁、原子替换和写前合并。不同会话的折叠、
  滚动、Inspector 与输入历史不会因后启动的进程保存状态而被覆盖。
- 关闭一个 Bridge 不会关闭其他 Bridge。每个进程在关闭时只释放自己的 Engine 资源。

## Git 工作区

多个会话可以读取同一目录。若任务会修改代码，建议为会发生文件交叉写入的任务创建不同
Git worktree，再把各 worktree 路径传给 `/parallel`。同一目录的多个进程不会自动合并
文件修改或解决 Git 冲突。

## 验证

真实验收脚本会同时启动 10 个 Bridge，共享一个 SQLite 会话库，为每个 Bridge 创建新会话，
确认 10 个会话 ID 唯一，终止其中一个后检查另外 9 个仍能响应，并核对数据库使用 WAL：

```powershell
uv run python scripts/verify_terminal_parallel_sessions.py
```

定向测试：

```powershell
uv run pytest tests/unit/test_parallel_sessions.py tests/unit/test_command_index.py tests/unit/test_ui_bridge.py -q -k "parallel or slash_new or command_index"
node --test frontend/terminal-ui/test/ui-state-store.test.js
```
