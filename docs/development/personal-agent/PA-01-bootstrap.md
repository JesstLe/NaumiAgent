# PA-01：事项资料库首次并发初始化

日期：2026-11-05  
基线：`40a40385c80b070aba5095d2fce863af2ae5ef2a`  
事务：`.naumi/transactions/pa01-bootstrap-20261105/`

## 1. 问题与边界

PA-02 的相关回归暴露了既有 ItemStore 的首次并发初始化竞态：多个独立实例同时设置 WAL、
建立 schema 和 witness 时，读者可能观察到中间状态。实例内部的 `RLock` 不能协调其他实例或
进程。另一个确定性交错是：读者先观察到 DB 不存在，写者完成整个初始化，读者再观察到 witness
存在；不能据此把合法的跨时刻采样判成原数据库丢失。

本次只修改 `PersonalItemStore` 的初始化、文件身份校验和 SQLite 事务边界。
schema 仍为 **v1**，不改事项定义、request receipt、CAS、调度、Job/Run 或人工已读协议，
不扩展工具权限，也不重放历史模型、Docker 计算或外部动作。

## 2. 实现

- 新增永久 `.personal-items-bootstrap.lock`，Windows 使用真实 `msvcrt` 一字节锁，
  POSIX 实现使用 `fcntl.flock`。进程退出由操作系统释放锁，锁文件不 unlink。
- 在取得锁前后核对文件描述符与当前路径的同一性、类型、链接数及 reparse 属性。
  空锁文件先取得 OS 锁，再写入单个 NUL；不截断既有内容，未知内容拒绝继续。
- 建立 WAL、schema、owner/instance metadata 和 witness 只发生在 bootstrap guard 内。
  witness 必须是与 instance ID 一致的 32 字节 ASCII；写入使用 flush/fsync。
- `_observe_database()` 对无锁样本同时复核成功和失败：观察期间若出现 bootstrap 锁，
  废弃该样本并重新取得 guard。`list`、`identity`、直接 connection 路径共用这一逻辑。
- `_seen_database` 仅在 schema/owner/instance/witness 验证且初始化事务 commit 成功后设置。
  不会因单独看见 DB 文件就承认它有效。
- 既有 DB 在 guard 内只使用 deferred read transaction，提交后释放 bootstrap 锁和实例锁，
  再进入业务 `BEGIN IMMEDIATE`。SQLite 继续独立仲裁业务写入、CAS 与幂等；
  等待某个业务 writer 的进程不会持有 bootstrap 锁挡住第三个 reader。
- OS 锁超时与 SQLite BUSY/LOCKED 返回不同的中文临时忙提示；错误 code 保持原
  `storage_error`，不扩大 service 层的恢复或重试权限。
- 预存空库、损坏/异构 schema、错 owner、丢 DB、缺/错 witness 都不自动修复或重建。

## 3. 验证方法

新增 `tests/unit/test_personal_item_bootstrap.py`，共 18 个测试函数、26 个参数展开案例：

- 8 个独立实例及 8 个真实 OS 进程竞争同一请求，检查唯一事项、回执和 instance ID。
- 6 个进程竞争两个 owner；6 个不同 payload 竞争同一 request ID，只有合法 winner。
- 在真实 DDL 后暂停初始化，读者等待 schema/witness 完成；强制结束 holder 后释放锁。
- 业务事务 yield 不持 bootstrap；三进程 writer/waiter/reader 交错仍能读取已提交状态。
- 实际 OS 锁超时、描述符与路径替换、硬链接、junction/reparse、未知锁内容及外部零写。
- 空/坏/异构 DB、已见 DB 丢失、witness 丢失/独存，以及 DB/witness 跨时刻采样的三个入口。

基线和过程中出现的失败日志均保留。第一次三方观察测试在
`sqlite3.connect/handle` audit 阶段安装 trace，此时 Connection 尚未初始化，实际抛出
`ProgrammingError: Base Connection.__init__ not called.`。测试改为真实 `_validate_schema`
完成后安装 trace，不替换 SQLite 操作、不降低产品断言。该 instrumentation 修订不被描述成
产品修复。

集中回归、兼容读回和回滚的确切命令、输入、字面输出及退出码以本事务
`VERIFICATION.txt` 为准；此前 PA-01/PA-02 的失败与收费记录保持原样。

本轮集中组终态为 **1960 passed / 5 skipped / 2 warnings in 498.05s**，exit 0。
`ruff check src/ tests/unit/test_personal_item_bootstrap.py` 输出 `All checks passed!`；
实际 import 输出 `IMPORT_OK; ITEM_SCHEMA_VERSION=1`，均 exit 0。
5 项 skip 中 2 项属于既有 ArtifactStore symlink 案例，3 项属于新 bootstrap 案例：
Windows 拒绝替换已锁文件一次，以及缺少 symlink 创建权限两次。
两条 warning 均为 websockets 的 deprecation，不是用 skip 隐藏产品失败。
该组覆盖全部 `test_personal*.py` 及记忆、压缩与命令相关组，不代表全仓库 pytest 通过。

## 4. 真实历史数据兼容

使用 PA-01 原真实 provider/Docker 产生的事项资料库作只读来源。
在独立副本上重放原 create 请求，再由 8 个实际 OS 进程并发读取同一幂等回执，
最后从新进程冷读。比较 schema、instance、所有表的逻辑摘要、已有 execution references，
以及原来源文件的字节与时间戳。既有 Run、Job、publication 和 delivery 只能读回，不再启动。

实际结果为 `COMPATIBILITY=PASS`，exit 0：schema v1、原 instance 和全部表逻辑摘要一致，
4 个事项、11 个修订、11 个请求回执、3 个 intent、13 条 reference history 及 3 个 reference head
均保持不变；3 组既有完成引用保留。原 create 请求返回原 closed / revision 2 事项，8 进程
结果一致，新进程冷读通过；唯一新增是副本上的 1 字节 bootstrap 文件。
原 DB/witness/WAL/SHM 的 bytes、SHA256、mtime 全部不变。

验收工具的首次启动因相对解释器路径而未创建子进程；绝对路径启动后，纯 Store import 的
LiteLLM 元数据 DNS 尝试被审计 hook 在解析前阻断。两次失败记录均保留。
独立 import probe 确认官方 `LITELLM_LOCAL_MODEL_COST_MAP=True` 使用本地数据并消除该请求后，
新进程在不清空审计计数、不重建副本的条件下，从已验证 checkpoint 继续。
成功续跑及其 workers/cold reader 为 0 provider / 0 network attempt / 0 forbidden read /
0 Scheduler/Job/Engine start。最初被阻断的 metadata 尝试不改写为零。

该验证属于真实历史数据的 **Store 兼容层**，不是新的模型、HTTP UI、外部账户、部署或
其他操作系统验收。本次没有修改 UI 或执行路径，不把旧浏览器验收重新计为本次完成项。

## 5. 自我审视与未完成项

- 独立静态复核发现的跨时刻失败窗口已用三入口确定性测试复现并修复；最终复核针对当前源码 SHA。
- Windows 是本轮实际执行平台。POSIX 分支尚未通过本轮完整模块实测。
- Windows 若拒绝创建 symlink 或替换已锁定文件，相应案例记录为 skip，不宣称通过。
- 本次不修改 MemoryStore；相似结构的静态观察不等同于已确认的 MemoryStore 缺陷。
- PA-01 整体、其他 PA 模块和完整视频能力仍按原验收条件推进，本次可靠性修复不代表总目标完成。
- 回滚仅恢复白名单源码与文档；不降级或回滚任何真实数据库、Run、模型用量或外部状态。

## 6. 可复用核验指令

```text
核验事项资料库首次并发初始化：保留原 owner、instance、schema 和 request_id；
用真实进程协调初始化与业务事务，不用重放模型来测试 Store 幂等。
发现超时或缺回执时读取原身份并保留 UNKNOWN，不建立新事项替代原请求。
对历史资料只读取证，在独立副本测试，证明原文件及已有执行引用没有改变。
不要创建、重命名或预置任何 Agent。
```
