# PA-04：周期事项持久事实与 OwnerJournal

日期：2026-11-05

## 本模块负责什么

`RoutineStore` 保存周期定义、occurrence、预算预留、逐模型请求记录、游标、通知批次及原请求回执。
单个 `write(callback)` 或 `mutate(request_id, operation, payload, callback)` 使用一个 SQLite
`BEGIN IMMEDIATE` 事务；service 可以在 callback 内同时写入预算、occurrence、游标和请求回执，
任何异常都回滚整笔业务写入。store 不执行模型、调度器、文件处理或设备通知。

Job、Run、报告、publication、inbox、人工已读仍由原有模块持有，store 不复制运行状态机。
新的周期经 service 固化 occurrence 后，继续调用既有单次 Item 执行链。

## 固定内部规格

`owner_journal.py` 提取原 `ConnectionStore` 的底层能力；`JournalSpec` 只接受代码中固定的内部规格，
不是外部请求的可配置 SQL、表名或文件路径。`routine_store.py` 的固定规格为：

| 项目 | 值 |
|---|---|
| SQL namespace | `routine` |
| 数据库 | `personal_routines.db` |
| 实例 witness | `.personal-routines-v1` |
| 初始化锁 | `.personal-routines-bootstrap.lock` |
| 父记录字段 | `routine_id` |
| schema user_version | `1`，仅针对全新的周期 store |
| kinds | `routine / occurrence / budget / calendar / preview / notification / batch / preferences / followup / workspace / call` |

周期 store 没有 `connection/action/event` 别名。所有类型都写入自己的真实 kind；逐 transport
费用预留使用 `call`，不是把外部请求伪装成预算行。

表为 `routine_meta / routine_records / routine_history / routine_requests / routine_leases /
routine_lease_history`。records 是可更新的 CAS head；history、requests、meta、lease_history
均由 SQLite trigger 禁止 UPDATE/DELETE。schema 校验核对全部表定义、trigger 定义及显式索引定义，
只把表名相同但约束被替换的数据库也视为校验失败。

## 事务接口

```python
store = RoutineStore(base_dir, owner_key=owner, expected_instance_id=original_instance)

def admit(tx):
    budget = tx.get("budget", budget_id)
    tx.put("budget", updated_budget, expected_revision=budget["revision"])
    occurrence = tx.put("occurrence", frozen_occurrence, expected_revision=0)
    routine = tx.get("routine", routine_id)
    tx.put("routine", advanced_cursor, expected_revision=routine["revision"])
    return occurrence

result = store.mutate(request_id, "admit_occurrence", original_payload, admit)
```

- `read(callback)`：只读连接。尚未创建的 store 给 callback `None`，不会创建目录、DB、witness 或锁。
- `write(callback)`：写入前完成 owner、schema、witness 与实例核验；callback 得到事务对象。
- `mutate(...)`：请求摘要绑定 operation 与完整 payload；重复原请求返回原不可变结果，内容不同则冲突，
  不执行 callback。预算与游标不因重试再次扣减或推进。
- `tx.get(kind, id, revision=None, missing=False)`：当前或明确历史修订；head/history 缺口不会当成不存在。
- `tx.put(kind, value, expected_revision=...)`：从 revision 0 创建或用当前 revision CAS 更新。
  返回值与读取值均是 JSON 重新构造的独立值；调用方修改返回对象不改写持久原件。
- `tx.request / save_request`：在同一事务保存、读取原请求回执。
- `tx.list(..., after=id)`：使用 `WHERE id > ? ORDER BY id LIMIT ?` 的真正键集分页。
- `tx.page(..., limit=..., after=...)`：返回 `{records, next_after}`，多取一条判断是否仍有下一页。
  不用 OFFSET，也不把首批 1000/10001 行当成整个数据集。

service 负责类型字段、生命周期、occurrence immutable identity、成本计算和许可规则；store 负责
这些事实的原子性、不可变修订、CAS、原请求和存储完整性。事务接口不是开放给模型的任意资料写入口。

## 哈希、原件与历史

新周期事实的 checksum 同时绑定 journal namespace、owner_key、kind/原请求身份与完整 JSON。
当前 head 要求 revision 为正整数，history 从 1 开始连续且与最新 head 完全一致。周期 store
读取当前 head 时也校验早期修订的完整 checksum，不把已损坏的旧修订隐藏在最新快照后面。

数据库 meta 中的随机 instance_id 必须与 witness 字节一致。store 一旦观察到原库，后续操作
会固定该 instance。service 在 DB 目录之外持久保存自己的实例 witness，冷重启传入
`expected_instance_id`；这样整个周期目录同时丢失或被另一份有效库替换也会失败关闭。
**仅靠同目录内的 DB+witness 无法辨认两者同时丢失后的空目录，service 的外部实例绑定是必需接缝。**

数据库、witness、初始化锁、所有父目录以及周期 SQLite 的 WAL/SHM/journal sidecar 都拒绝
符号链接、Windows reparse/junction 和文件硬链接。校验文件句柄与当前名字的文件身份，
不会截断未知锁文件或用新库覆盖已有空文件、损坏库、未知 schema。

## 跨进程与租约

bootstrap 使用 OS 文件锁协调 schema+witness 的首次创建。它只覆盖初始化观察，不持有整个业务事务。
业务写入以 SQLite `BEGIN IMMEDIATE` 为唯一仲裁者；一个等待写锁的进程不会通过初始化锁阻塞第三个
WAL reader。空库 read callback 同样在释放实例锁、bootstrap 锁后才运行。

```python
lease = tx.claim("routine_tick", routine_id, holder, now=now, ttl=60)
tx.fence("routine_tick", routine_id, holder, lease["fence"], now=now)
tx.release("routine_tick", routine_id, holder, lease["fence"], now=now)
```

`claim` fence 严格递增。旧 holder、旧 fence 或已过期租约均不能继续操作。
阶段为 `claimed / sent / unknown / finished`；sent/unknown 不允许回退到 claimed。
`once=True` 遇到已发送或未知的原请求，即使租约过期也不重新 claim 发送。
`release` 是经过当前 holder/fence 核验的 finished 转换，不等于宣布业务成功或释放未知费用。
新的调度租约可以在 finished 后继续 claim；不可逆调用能否再次执行仍要查询原 `call` 事实和原请求。

周期 lease 的每次 claim/阶段变化都会追加 hash-chain receipt；读取时要求完整连续链与 mutable head
完全一致。删掉 lease head、改 holder、删掉早期事件或修改 receipt 都会失败关闭，不被当成新租约。

## 与旧 ConnectionStore 和 Item v1 的关系

`connection_store.py` 保留原 `ConnectionStore(base_dir, owner_key=...)`、
`ConnectionTransaction(db)`、`broken/busy/checked_path/ordinary/schema_sql` 接口。
原 ConnectionStore 的 SQL 表名、列序、表定义、trigger 文本、文件名、kind、parent 字段、user_version、
原 checksum 与原请求摘要算法不变，也不新增 lease_history 表。

`ConnectionStore` 仅作为固定 legacy spec 的薄包装调用同一底层。周期 owner-domain hash 和 lease
证明只对新的 RoutineStore 启用，不重写旧连接行。两个 store 可以位于同一个目录，也不会共享 DB、
witness、锁、request_id 域或 kind。

本模块不改 `personal_items.db`、Item v1 schema、旧 snapshots、旧 Run 或旧 receipt。
源码回滚保留新周期库，不作数据库降级，也不撤销真实费用。旧版执行器对新周期子 Item 的停止 gate
由 PA-04 domain/Artifact 授权层负责，不能仅凭底层 store 回滚推导完成。

## 验证与当前边界

集中验证集包括原 `test_personal_connection_store.py`、连接/日程动作相关回归以及新的
`test_personal_routine_store.py`。新增集覆盖多事实原子提交、原请求幂等、CAS、真实多进程竞争、
初始化/读写锁分离、键集遍历 10013 行、owner mismatch、schema/trigger/index 替换、历史缺口、
原件缺失、全目录丢失后的外部 instance pin、完整有效库替换、lease head/history 损坏、硬链接和
Windows junction，以及原 ConnectionTransaction 直接构造和 legacy checksum。

本模块首次集中运行：`319 passed, 6 skipped in 38.11s`，exit 0。6 个 skip 是两套 store 各
3 个当前 Windows 未启用的符号链接创建用例；文件硬链接与原生目录 junction 用例均实际运行。
命令为：

```powershell
.venv/Scripts/python.exe -m pytest tests/unit/test_personal_routine_store.py tests/unit/test_personal_connection_store.py tests/unit/test_personal_calendar_actions.py tests/unit/test_personal_connection_interfaces.py -q --disable-warnings --tb=short
```

最终相同模块集复跑为 `319 passed, 6 skipped in 37.73s`，exit 0；日志为
`routine-store-tests-final.txt`。复跑前所认领三个 source 文件与新测试的 ruff check 输出
`All checks passed!`，exit 0。

另在隔离目录加载事务中封存的**原 ConnectionStore 源码**，执行原实现写 revision 1 → 新实现读取并写
revision 2 → 原实现读回并写 revision 3 → 新实现读回，同时校验原 create request 的精确重放。
新旧 `_SCHEMA` 与 `_TRIGGERS` 逐字节相等；原 DB schema 未改，新增周期 DB 不影响旧 reader。
实际输出 `COMPATIBILITY=PASS SQL=BYTE_IDENTICAL TRIGGERS=BYTE_IDENTICAL HASHES=UNCHANGED`，exit 0。
脚本与逐字输出分别为事务目录内 `routine-store-legacy-compat.py / routine-store-legacy-compat.txt`。

最终测试命令、实际通过/skip 数与退出状态在本轮事务 `VERIFICATION.txt` 中记录；未跑完的层级不从本文件推导通过。
此底层测试不调用付费 transport，不读取账户凭据，不启动服务；周期 UX/真实端到端由整个 PA-04 模块独立验收。

## 主集成回读后的 sidecar 修正

首次宽回归的六进程 claim 用例出现过一次失败；真实库只保存了一个 winner，但当时完整 outcomes
没有被断言打印，因此不把该次失败的精确异常原因写成已经证明。随后独立发现并机械复现另一项
实际 race：`_paths()` 使用 `exists → is_file` 两次采样，SQLite 在中间合法删除 WAL 时会误报
`storage_error`。`sidecar_probe.py` 在修改前输出 `SIDECAR_PATHS=storage_error; EXISTS_RACE_TRIGGERED=True`。

主集成将路径类型检查改为单次 `lstat`，保留 symlink/reparse/hardlink 与稳定库/witness 检查；
相同 probe 修改后输出 `SIDECAR_PATHS=accepted; EXISTS_RACE_TRIGGERED=False`，两个 probe 均 exit 0。
新增三个真实临时 sidecar 窗口回归，并将六进程竞争作为四轮独立 fixture 验证，异常时保留完整
outcomes。最终状态仍以父事务集中回归为准，不抹掉原失败，也不放宽为“任何失败都可以”。
