# HAR-08.4b 生产 Sandbox Check Surface

## 1. 交付目标

让用户 `/harness check <id>` 与 Agent `harness_run_check` Tool 共享唯一生产执行链：Engine 权限决策形成持久
父回执，Service 精确复核后委托 ARC-04.3b 生成一次性 Shell authority，HAR-08.4a Runner 在隔离 Git snapshot
中执行，并在任何终态释放 Worker 与 Lease。

## 2. 权威调用链

1. Slash 只构造带稳定 session run id 的 `ToolCall`，不直接调用 Service；
2. Engine 按 policy、allow-once 或 bypass 记录当前调用父回执，并声明唯一可委托 Tool `bash_run`；
3. `permission_context` 通过任务局部 `ContextVar` 在实际 Tool invocation 期间绑定可撤销父回执 capability；
   返回或异常时除 reset 外还会使共享 capability 失活，已继承上下文的后台 Task 也不能继续读取授权；
4. HarnessService 重新核对 parent tool、run、完整参数 digest、执行终态与委托范围，任何不一致均 fail closed；
5. Composer 签发精确子回执、Worker incarnation、snapshot Tool lease、delegated ExecutionGrant 与 ToolJob；
6. Runner 校验 immutable admission 后执行，保留有界 artifact 与 lifecycle receipt，删除 snapshot；
7. Service `finally` 调用幂等 cleanup；Lease 与 Worker 清理相互独立尝试，清理不完整显式失败。

## 3. 用户体验与兼容

- 成功/失败输出沿用统一中文 Harness renderer，并附 ToolJob、生命周期回执和有界日志路径；
- Slash 与 Agent Tool 看到同一状态与输出，不存在“手动命令绕开权限”的第二实现；
- Engine 生产装配始终注入 Sandbox Runner/Composer/context provider；未注入三项依赖的独立 HarnessService
  仍保留旧 Runner，作为内部兼容和现有单元测试入口，不代表生产 fallback；部分注入直接拒绝构造；
- bypass 只改变父决定来源，仍保留 session/run/参数/委托/过期边界，不伪造无限期下游授权。

## 4. 验收标准

- 真实临时 Git 仓库经 Slash 完成 trust→check，输出通过且源工作树无污染；
- 持久 Store 同时存在 `harness_run_check` 父回执与 `bash_run` 子回执，父子 digest/identity 可追溯；
- ToolJob artifact 包含真实命令输出，用户回执可定位 job、lifecycle 与 artifact；
- 执行后 Worker 无 active registration、Tool lease 为 released、sandbox 根目录为空；重复 release 幂等；
- Engine Tool 内能读取当前精确回执，退出 invocation 后 context 为 `None`；
- ruff、py_compile、Engine 权限、Slash 真实链路与 Shell Worker 小模块测试通过。

## 5. 未完成与后续依赖

- HAR-08.4c 已补精确 Git revision snapshot；Harness Eval Suite、重复 Batch 和 Baseline Comparator 仍走
  各自既有执行器。下一消费者应由 EVO Request 编排本链，而不是复制 subprocess；
- session grant 的后续调用缺少可验证当前-call 派生事实，继续 fail closed；
- macOS 已有真实 sandbox 证据；Linux bwrap 与 Windows 隔离后端需要各自 CI/机器证据；
- 当前 Worker 为每 Job 短寿命进程，不等同 ARC-04.6 长寿命 Supervisor、队列背压或 crash recovery。
