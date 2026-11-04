# F06/F07 浏览器任务控制修复

## 修复范围

本模块修复默认并行运行中的浏览器实例归属，以及人工接管、恢复、终止之间的并发状态竞争；不调用用户真实浏览器，不改变对外任务状态枚举。

1. 建立 `run_id -> runtime` 映射，执行、接管、恢复、abort记录事件使用同一实例。隔离运行实例缺失时明确报错，不回退到共享浏览器。
2. 每个任务的恢复、人工接管与最终浏览器stop共用异步操作锁，避免两个恢复同时重启浏览器，或stop与未结束的模式切换并发。
3. 同步abort仍立即设置终止标记并解决等待future；两个恢复await之后都检查终止状态、future是否完成及其同一性，最后提交状态与完成future之间没有await。
4. 等待输入流程在异步准备之前建立future；准备期间abort会直接返回终止结果，不再发布新的等待状态。timeout回调仅能终止仍属于本次等待的future。
5. waiting状态在heartbeat准备完成后发布；终态在runtime.stop与heartbeat.finish完成后发布。两个原有心跳测试因此无需削弱断言即可通过。
6. 结果已经产生且开始清理后，明确拒绝新的abort，避免先回应“已终止”随后又把completed写回；此时没有剩余任务动作。清理结束后释放runtime映射、future、timeout、锁和并发槽位。

## 基线与回归

测试启动环境固定：主仓已有Python虚拟环境，`PYTHONPATH=E:\Workspace\NaumiAgent-fix-core-20261104\src`；HOME、USERPROFILE、个人数据和pytest临时目录隔离到外部证据目录。

```powershell
E:\Workspace\NaumiAgent\.venv\Scripts\python.exe -m pytest tests/unit/test_task_runner_control_races.py tests/unit/test_task_runner_heartbeat.py -q --tb=short -p no:cacheprovider
```

修改前：`8 failed, 5 passed in 3.80s`，exit 1。包含两个await中的InvalidStateError、双恢复导致两次浏览器切换、错误runtime、接管准备时abort后无限等待、stop与切换重叠，以及原有两个heartbeat状态发布竞态。

修改后整模块：

```powershell
E:\Workspace\NaumiAgent\.venv\Scripts\python.exe -m pytest tests/unit/test_task_runner.py tests/unit/test_task_runner_control_races.py tests/unit/test_task_runner_heartbeat.py -q --tb=short -p no:cacheprovider
```

- 初次集中验证：`97 passed in 3.58s`，exit 0。
- 补充并行runtime独立控制、人工切换中abort、无runtime拒绝fallback、waiting发布边界及finalizing时late abort回归后：`102 passed in 3.61s`，exit 0。
- Ruff：`All checks passed!`；compileall与git diff --check通过。

## 自审

- 没有只在set_result前加done检查：模式切换和心跳两个await都重新校验，stop也参与串行化。
- `_on_needs_input`的等待future在发出waiting事件前已存在，避免可见状态与控制通道错位。
- 保留已有attached会话单槽约束、runtime创建失败清理、并行上限与历史修剪回归；终态映射按实际实例生命周期释放。
- 不把“status先变completed、heartbeat尚在running”通过改测试期望为running掩盖，实际调整发布顺序。
- 浏览器依赖使用事件门控/可控runtime，执行的是实际TaskRunner、临时TaskRunStore和真实心跳SQLite。此结果不是原生浏览器端到端验收，也不证明任意外部浏览器服务异常都可恢复。
- 本模块没有新增终止活跃模型请求的机制；abort沿用现有协作取消协议，本次保证状态和人工控制不会覆盖终止结果。

证据：`E:\Workspace\NaumiAgent-fix-core-evidence-20261104\browser-baseline.txt`、`browser-modified.txt`、`browser-final.txt`。

## 复审补充

- 启动失败进入清理也注册 finalizing 标记，拒绝在心跳完成等待期间覆盖失败结果；对应模块 103 passed。
- 独立复审复现了 `runtime.stop()` 期间取消导致槽位/runtime/control 滞留。最终清理抽为独立 task，经 `finish_cleanup()` shield；重复取消等待资源与状态收口后传播。槽位、控制映射、等待 future 和定时器由 finally 释放，持久化异常也不占住内存槽位。
- 修复后模块 104 passed in 3.73s；新增重复取消回归验证 stop 完成、终态落地、全部映射清空及 active_slots=0。证据 `browser-finalization.json`、`review/repro_finalization_cancel.txt`（后者为修复前复现）。
