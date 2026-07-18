# ARC-01.4b2j Shell Worker Runtime Paths

## 交付目标

Composition Root 为 ARC-04.3a 提供三个规范化、跨平台、惰性路径，而不是让 Worker 从当前目录或临时环境变量
推导 authority 边界：

- `shell_worker_runtime_dir`：本地认证 transport 的短寿命 socket/控制文件目录；
- `shell_worker_sandbox_dir`：HAR-08.4a 的单次 Git snapshot 根目录；
- `shell_worker_artifact_dir`：命令输出 artifact 的持久根目录。

三者均位于 `runtime_data_dir`，互不重叠；`build_runtime_paths()` 只计算路径，不创建目录。真正执行检查时由
Runner/Transport 以 `0700` 惰性创建，因此单纯启动 Engine 不产生空目录或执行副作用。

## 验收证据

- 默认路径与自定义 `runtime_data_dir` 都得到确定性绝对路径；
- RuntimePaths 对相对路径、逃逸路径和错误类型 fail closed；
- Engine 组合测试证明 Shell Transport、Coordinator 与 Sandbox Runner 共享同一 typed paths；
- Engine 构造后，上述三个目录仍不存在。

## 边界

路径所有权不等于 OS 隔离、授权或清理证明。隔离合同属于 ARC-04.3a，快照生命周期属于 HAR-08.4a；后续
Supervisor 若接管长寿命 Worker，仍必须消费这些路径而不能另造隐式目录。
