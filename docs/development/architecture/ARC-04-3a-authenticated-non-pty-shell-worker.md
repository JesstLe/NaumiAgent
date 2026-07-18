# ARC-04.3a 认证本地 Non-PTY Shell Worker

## 1. 交付目标

把 ARC-04.2b/2c 的不可变 ToolJob 与生命周期回执接到一个真实、默认断网、非 PTY 的本地 Shell 执行器。
命令 payload 只能在 `dispatched` 回执持久化后发送；Worker 开始前必须写 `running`，终态复用同一 receipt 链。

## 2. 执行合同

- `ShellCommandSpec` 只接受绝对 executable、相对 cwd、排序后的非敏感环境变量、Workspace manifest SHA-256、
  独立 artifact 根/文件名，以及 wall/output/memory/CPU 上限；当前只接受 `network_disabled=true`；
- `ShellCommandRequest` 绑定 Job、Worker id/instance/epoch 与 Worker contract digest，transport 消息再绑定一次性
  nonce 和 request digest；
- `AuthenticatedLocalShellTransport` 以 multiprocessing `spawn` 建立独立进程，并用
  `multiprocessing.connection` 的 HMAC challenge-response 认证。macOS/Linux 优先 AF_UNIX，路径过长时回退
  loopback AF_INET；Windows 合同预留 AF_PIPE；
- stdin 固定为 `DEVNULL`，不分配 PTY；stdout/stderr 直接写 artifact，只返回最多 64 KiB tail、字节数、输出
  SHA-256 与 artifact manifest SHA-256；
- 取消、wall timeout、输出或内存超限终止完整进程组；macOS/Linux 使用 session/process group，Windows 路径
  使用 `taskkill /T /F`；
- `ShellWorkerCoordinator` 是唯一 dispatch producer：消费 ToolJob authority、lifecycle authority 与实时 Worker
  registry；终态重放只返回既有 receipt，`dispatched|running` 重入要求 reconcile，禁止重复执行。

## 3. OS 隔离

- macOS：使用系统 `/usr/bin/sandbox-exec`，默认断网，只允许 Workspace/Artifact 写入；Home 目录禁止枚举，
  并按路径树拒绝读取 Workspace、Artifact 与受信 executable 之外的 Home 内容；系统级只读依赖仍可读取；
- Linux：仅在 `bwrap` 可用时运行，`--unshare-net`，根文件系统只读绑定，Workspace/Artifact 可写；`bwrap`
  缺失时 fail closed；该合同不宣称隐藏所有宿主只读文件；
- Windows：当前没有已证明的默认断网适配器，因此 fail closed，不以普通 subprocess 冒充隔离 Worker。

## 4. 验收证据

- 真实认证 Worker 执行 Python 命令、写入 Workspace，并持久化可校验 artifact；
- 越界写入、Home 枚举和 loopback 网络访问被 macOS 沙箱拒绝；
- 取消会杀死孙进程；wall/output/memory 上限分别产生 typed 终态；
- 真实 grant -> ToolJob admit -> dispatch -> running -> success 链完成，终态 replay 不再次发送 payload；
- cwd 逃逸、相对 executable、敏感环境变量在进程创建前拒绝；
- 目标测试 `tests/unit/test_shell_worker.py` 与相邻 ToolJob/Runtime Composition 测试通过。

## 5. 诚实边界与下一步

- 当前是每 Job 一个短寿命本地 Worker 进程，不是长寿命 daemon；没有 heartbeat producer、crash-loop、drain、
  upgrade 或 Supervisor；
- 没有 PTY、交互 shell、队列/backpressure 或 100 并发容量证据；
- HMAC 保护本机 transport 会话，不是跨主机 mTLS 或硬件身份；
- Windows 必须先完成可验证的 restricted token/job object + 网络隔离适配，Linux 还需在 CI 真实验证 bwrap；
- 下一最小 ARC 前置应服务 Browser/Agent Worker 或 Supervisor，而不是扩张 Shell 功能；HAR-08.4a 已可消费本
  Worker 跑一个 Profile check。
