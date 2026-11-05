# Agent 控制中心：执行后端与验收合同对齐

日期：2026-11-06

## 目标与根因

Personal Agent 完整目标仍包含原有 24 项能力。本次只修复阻塞仓库回归的
Agent 控制中心验收合同，并补充当前生产独立 Worker 的真实本机链路；
没有新增固定职业、预置 Agent 或改变用户创建身份的规则。

基线提交：`1d71ef3ad5ceb40289a750db473c04d29f130989`。
事务：`.naumi/transactions/control-backend-parity-20261106/`。
修改前冻结并重开 3177 个跟踪文件的原字节、SHA256 及独立回滚副本。

旧 E2E 在父进程 monkeypatch 两个 `BaseAgent.execute()`，随后等待其中的
`target_started`。当前默认 composition 给 `AgentWorkerProcessFactory`
传入 `config.models`，故 `model_execution_enabled=True`，manager 选择
`_execute_independent_agent_job()` 而非被 patch 的 `agent.execute()`。
事件不会置位，延长等待时间不解决后端不匹配。

原源码副本实际运行原 E2E：call 阶段在原 1 秒等待处 `TimeoutError`，
stdout 为 `F`，退出 1。原失败后的 teardown 未自然结束，观察器保留原始
pytest call 报告后只结束本次进程树；此结果不算自然退出的完整 pytest 总结。
外部网络审计器在父/子 Python 中生效，非本机网络尝试数为 0。

## 实现

### 原有 embedded 场景

`tests/e2e/test_agent_control_center.py` 明确创建使用原 Engine、原
`AgentJobStore` 和原 heartbeat factory 的 `SubAgentManager`，不传独立
Worker factory；同时同步分析工具的 manager 注册。原事件等待仍为 1 秒。
断言默认生产 factory 仍具备独立执行能力，且本场景 snapshot 的
`worker_backend` 明确为 `embedded`。

该场景继续验证真实 SQLite Job、JSONL bridge、Node reducer/render、Textual、
单任务停止与兄弟任务完成，不把 BaseAgent 的受控执行当成真实模型。

### 默认 independent 场景

新增 `tests/e2e/test_agent_control_worker.py`：

- 显式本机 HTTP 模型协议 fixture 和假测试凭据，不读取真实账户凭据。
- 默认 composition 启动两个真实认证子 Worker；父进程 `BaseAgent.execute`
  失败哨兵用于检测意外退回 embedded。
- 两个 Worker 通过原父进程权限入口读取不同真实临时文件，各自进入事件屏障。
- bridge 请求 snapshot、停止目标、再次停止；实际 Node 与 Textual 读取相同状态。
- 目标执行取消而持久 Job 保持 `running`，失败码为
  `agent_worker_interrupted_recovery_required`，不伪造已完成的持久取消。
- 兄弟任务不受影响，释放自己的屏障后完成，并从新建 `AgentJobStore` 按原
  result SHA 认证读回原结果；不重新运行或重写原身份。
- 收尾关闭原进程与本机服务。模型 fixture 不是外部真实模型或 Google 验收。

## 验证记录

集中模块回归、全仓 `-x`、静态检查、原件副本回滚及后续改动保护的最终
字面命令、输入、输出与退出码记录于事务 `VERIFICATION.txt`。
测试失败保留原报告，修正后使用新的记录名，不覆盖失败证据。

- 最终集中回归：**58 passed, 1 skipped in 98.88s**，退出 0。
  原 E2E 保留 1 秒等待，独立 Worker 新场景通过；既有 Windows/POSIX 条件跳过保留。
- 首轮相邻回归为 54 passed / 1 failed / 1 skipped：既有入口测试将 factory
  次数硬编码成 3，实际 CLI 已有 TUI、chat、run、安装 daemon 四条有效入口。
  冻结基线同样复现 4 != 3。改为 AST 检查四个确切函数和 API lifespan 各自的
  factory 调用，并增加注释/格式变化与直接构造拒绝的测试；没有放宽架构预算。
- 扩大到既有全源码架构扫描时，发现原 `personal/engine_execution.py` 直接
  构造隔离 child Engine，不符合 composition-only 规则。冻结基线也复现该失败；
  本模块不改该产品路径，不把这次扩大回归报告计为通过。
- 全仓 `tests/ -x` 已越过原控制中心失败，两个 E2E 通过后止于
  `tests/e2e/test_live_api.py::test_simple_qa`。既有环境变量启用了真实模型测试，
  配置模型返回 HTTP 400：`properties.operation: type is not defined`。
  全仓不通过，保留原 failed Run、session 和响应，计费 UNKNOWN，重放 0。
  此调用不能被归类为本机模型 fixture。
- 观察器阻止直接非本机 socket，但允许的本机代理可转发远端请求。
  `full-repo-01.json` 通用 input 文案中的“fixture credentials only”对这条
  全仓命令不成立；实际情况以上述原报告及只读身份对账为准。
  socket guard 的 0 次阻断不是 0 次 provider 请求的证明。

后续具体修复点：`item_tools.py` 的字符串 enum 节点和
`connection_tools.py` 的 operation const 缺少显式 `type`。修复应保持原
JSON Schema 接受集合、操作分支和业务权限不变，不重试原失败的真实请求。

## 自审与未完成项

- 修改的是过期测试合同，不是用测试修改冒充生产启动性能修复；生产源文件不变。
- 新场景特意区分内存执行取消与持久执行待恢复，不以绿色 UI 覆盖未知副作用。
- 本次没有外部模型、真实 Google 账户、跨设备、部署或容量验收。
- 原有 24 项能力和 PA-06 的 partial 状态保留；本地模块测试不等于完整产品完成。
