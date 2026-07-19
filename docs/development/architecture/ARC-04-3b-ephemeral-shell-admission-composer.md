# ARC-04.3b 一次性 Shell Admission Composer

## 目标

把 UI-12.3b2 的精确子授权组合成 ARC-04.2/4.3 可执行的唯一 authority 链，不让 Harness 或 UI 自行拼装
Worker、Lease、Grant、ToolJob 身份。

## 合同

- 为每次 Sandbox check 注册新的本地 Tool Worker incarnation；epoch 从 Registry 历史单调递增；
- Worker contract 只声明 non-PTY、进程树取消、临时 Workspace、默认断网、环境白名单、资源上限与 artifact
  digest，资源 envelope 由精确 `ShellCommandSpec` 生成；
- 在 snapshot workspace 上取得同一 parent run 的 Tool lease，owner 固定为 Worker instance；
- 通过 UI-12.3b2 签发 `bash_run` 子回执，再用 snapshot workspace authority 签发 delegated ExecutionGrant；
- ToolJob admission 消费同一 Registry、Lease、Grant、health 与 isolation requirements；Shell request、dispatch id
  和 Coordinator 全部绑定该不可变 Job；
- compose 中途失败会尽力同时释放 Lease 与撤销 Worker；调用方完成执行后必须调用幂等 `release()`，再次
  fencing 两项 authority。单项清理失败不会跳过另一项，失败会显式上抛；释放不会删除 artifact 或 ToolJob receipt。

## 验收证据

- 真实 SQLite Store 完成 parent→child→register→lease→grant→admit；
- admitted ToolJob 参数与 Shell spec canonical payload 完全相同；
- active Worker/Lease 在 compose 后可读取，release 后分别变为 revoked/released；
- Composer 由 Engine Composition Root 注入共享 Registry/Harness/Permission/Grant/ToolJob Store 与 Shell
  transport，构造 Engine 不创建数据库或执行目录；
- Shell Worker 与 Runtime Composition 小模块测试通过。

## 边界与下一步

- Composer 只负责 admission/cleanup，不执行 payload，也不是长寿命 Supervisor；
- 当前同一 Engine 内用 registration lock 串行分配 epoch；跨 Runtime 竞争由 Registry fail closed，未来
  Supervisor 需要数据库原子 epoch allocator；
- HAR-08.4b 已让 `HarnessSandboxCheckRunner` 接收任务局部 parent receipt context，在 `finally` 调用 Composer
  release，并使 Slash 与 Agent Tool 共享生产路径；下一步不再扩张 Shell 前置，应转向 Sandbox Suite/Batch
  编排或 ARC-04.6 Supervisor 的最小消费者需求。
