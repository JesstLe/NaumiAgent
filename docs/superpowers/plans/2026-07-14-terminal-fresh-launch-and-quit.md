# Terminal UI 干净启动与本地退出实施计划

> 对应设计：`docs/superpowers/specs/2026-07-14-terminal-fresh-launch-and-quit-design.md`

## 任务 1：用失败测试固定 `/q` 本地退出语义

文件：

- 修改 `frontend/terminal-ui/test/state.test.js`
- 修改 `frontend/terminal-ui/test/index-process.test.js`

步骤：

1. 增加 `/q`、`/quit`、`/exit` 的状态单测，断言返回 `{type: "exit"}`、send 调用为零、outbox 为空。
2. 增加近似命令 `/query` 的回归测试，确认仍走普通提交。
3. 增加真实进程测试，输入 `/q` 后进程正常退出，调试协议只有 shutdown、没有 submit。
4. 运行名称过滤后的 Node 测试，确认新增测试先失败。

## 任务 2：实现本地退出动作

文件：

- 修改 `frontend/terminal-ui/src/state.js`
- 修改 `frontend/terminal-ui/src/index.js`

步骤：

1. 在 `handleSubmitText()` 最前面识别完整退出命令并返回本地动作。
2. 在 `submitComposer()` 中先处理动作；退出时不记历史、不持久化含命令的 composer。
3. 运行任务 1 的测试直到通过。
4. 检查退出路径仍复用现有 terminal/bridge 清理。

## 任务 3：用失败测试固定干净启动与显式恢复

文件：

- 修改 `frontend/terminal-ui/test/index-process.test.js`
- 按需修改 `frontend/terminal-ui/test/fixtures/fake-bridge.js`

步骤：

1. 将旧的“普通重启自动恢复 outbox”断言改为“普通重启不恢复”。
2. 在第一次进程中同时留下 queued outbox、composer 草稿与打开侧栏，第二次进程确认全部为默认状态。
3. 增加显式 `/resume` 的进程用例，确认只有 `session/replayed` 后恢复快照，且不自动 submit。
4. 运行名称过滤后的进程测试，确认显式恢复用例先失败。

## 任务 4：实现显式恢复门控

文件：

- 修改 `frontend/terminal-ui/src/index.js`

步骤：

1. 删除启动前对默认快照的自动应用。
2. 普通 session ID 变化只保存旧会话快照，不读取目标快照。
3. 对 `session/replayed` 无条件读取并应用目标 session 快照，再请求已打开面板的数据。
4. 保持输入历史的独立加载与保存逻辑不变。
5. 运行任务 3 及输入历史相关测试直到通过。

## 任务 5：窄范围验证与提交

1. 运行相关 `state.test.js` 测试。
2. 运行相关 `index-process.test.js` 测试。
3. 对修改的 JS 执行 `node --check`。
4. 执行 `git diff --check` 并审阅 diff。
5. 自我审视错误路径、相同 session ID 重放和近似命令。
6. 以英文提交该独立功能。
