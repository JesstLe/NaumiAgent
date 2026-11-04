# F02 代码执行生命周期修复

## 问题与实现

审计基线 `df444987` 的本地执行只有 TimeoutError 的直接 child.kill，没有取消清理，也没有管理后代；Windows 临时 cwd 清理还能覆盖原 CancelledError。

- 新增 owned_process：argv 不经 shell，启动 task 被 shield，启动过程中取消也不会丢失已创建进程；退出、异常、超时和取消共用清理。
- Windows：父进程持有 `KILL_ON_JOB_CLOSE` Job Object。可信 stdlib Python gate 等待 stdin，成功分配 Job 后才释放目标代码。后代继承 Job，直接父进程先退出仍可按 Job 终止全部进程；清理等待 active_processes=0。
- POSIX：独立 session/process group；复用 shell runtime 的进程组终止。即使根进程已退出也仍清理组。
- 输出清理有 3 秒期限；重复取消等待独立 cleanup task 完成后再传播 CancelledError。正常返回同样清理脱离 stdout/stderr 的后代。
- 不改变已有 shell runtime 调用者。本工具使用新的所有权边界，旧的仅 kill 单进程私有 helper 被删除，相应实现细节测试由真进程测试替代。
- Docker CLI 使用相同有界执行；每次使用唯一容器名，退出/超时/取消时定向 `docker rm --force <name>`。清理失败不作为成功结果。没有停止或清理用户其他容器。
- Windows gate 使用内联 stdlib 代码，不依赖额外脚本文件或审计目录。源码/轮子中的 Python 入口使用 sys.executable；冻结应用需独立 Python，缺失时在执行目标前明确终止，不把应用本体当解释器重启。

## 验证

基线命令（Python 使用主仓现有虚拟环境，PYTHONPATH 指向本修复工作树 src；HOME、USERPROFILE、个人数据与pytest临时目录隔离）：

```powershell
E:\Workspace\NaumiAgent\.venv\Scripts\python.exe -m pytest tests/unit/test_sandbox_lifecycle.py -q --tb=short -p no:cacheprovider
```

旧实现：`4 failed, 1 passed in 2.70s`，exit 1。失败分别是取消被 PermissionError 覆盖、超时遗留子孙进程、根进程已退出后遗留后代、正常完成遗留后代。

修复验证覆盖 sandbox、真实生命周期、owned process、既有 shell runtime 四模块。第一次集中验证：`50 passed in 9.88s`；随后补 frozen-interpreter fail-closed 测试，最终结果为 `51 passed in 9.63s`、exit 0，外部 `process-final.txt` 留存。Ruff输出 `All checks passed!`，编译检查退出0。

真实 Windows 用例包括取消（含重复取消）、启动期间取消、根进程仍在/已退出两种超时、正常完成遗留后代、Unicode、非零退出码和stderr。每个失败清理分支停止测试创建的进程。

## 自审与边界

- Job 分配失败时 gate 未开放，目标代码不会执行；测试已覆盖。
- Job handle由父进程持有，不依赖已退出的根PID查找后代；清理不因 returncode 非空而提前返回。
- Docker验证仅为命令协议和取消/超时顺序测试；本轮没有启动真实Docker容器，不声称完成容器验收。
- POSIX验证为进程组参数及已退出根进程清理契约；本机为Windows，未声称Linux/macOS真进程验收。
- 冻结应用的解释器缺失分支有单测，未构建/运行PyInstaller成品。
- 此修复提供进程所有权，不把本地fallback宣称为文件/网络/资源隔离沙箱；现有stdout累计内存策略未在本模块改变。

证据目录：`E:\Workspace\NaumiAgent-fix-core-evidence-20261104`。文件：`process-baseline.txt`、`process-modified.txt`、`process-final.txt`。
