# HAR-06.4 引用安全 Artifact GC 设计

## 用户问题

Session 删除协调已经保存 Check `artifact_path` 与 Evidence `artifact://` URI 快照，但物理文件
仍会长期残留。直接按字符串删除不安全：同一文件可能有两种引用写法、仍被其他 Session 使用，
也可能指向工作区外、普通项目文件、目录、设备文件或符号链接。

## 完成语义

一次 Session 删除只有在以下三部分均达到持久终态后才返回 `completed`：Session Store 已提交、
精确 workspace/session 的 Harness 行已提交、Artifact GC 状态为 `completed`。GC 的 completed
表示“所有快照引用均已分类处理”，不表示每个引用都被删除；共享、风险和非普通文件必须保留。

Harness DB v5 新增 `harness_session_artifact_gc`，保存 `pending|completed`、候选、删除、已缺失、
共享、风险、非普通文件和“无法解释存活引用”标记。v4 升级会为所有历史 reconciliation 回填
pending，因此旧 `records_committed` 也会被启动恢复发现。GC I/O 失败记录为独立 `artifact_gc`
tombstone，使用既有租约、退避、上限和跨进程恢复机制。

## 路径与引用规则

- Check 相对/绝对路径与 Evidence `artifact://` URI 统一解析到工作区物理路径。
- 百分号编码先解码；NUL、认证信息、query、fragment、未知 scheme、Windows drive 混入和路径
  穿越失败关闭。
- 只管理工作区 `artifacts/` 与 `.naumi/artifacts/` 两个根目录。`README.md` 等普通项目文件即使
  被错误登记为 Artifact 也不会删除。
- 多个 Check/Evidence 引用归一到同一路径后只形成一个候选。
- 符号链接、junction、嵌套挂载点、目录、FIFO、socket、设备文件和任何受管根目录外路径
  保留并计数。

## 并发与删除安全

GC 在 Harness `BEGIN IMMEDIATE` 事务内，以 256 行批次扫描同 workspace、其他 Session 的 Check
和 Evidence 引用。事务写锁阻止扫描完成后又插入新共享引用；共享候选不删除。两个 Session 并发
删除同一文件时，第一个看到另一个的引用并保留，第二个在前者行提交删除后才可删除文件。

POSIX 逐级使用目录 fd、`O_NOFOLLOW`、`lstat` 与相对 `unlink`，避免父目录符号链接替换导致越界；
Windows 使用严格 resolve、lstat 与删除前再次复核。文件已不存在视为幂等成功。部分文件删除后
进程退出时，SQLite 回滚 GC 状态，恢复重试把已删除文件归为 missing，再完成持久终态。

若任何存活引用无法可靠解析，本轮保守保留全部候选并持久记录阻断标记；不会为了提高回收率猜测
其含义。I/O 权限或文件系统错误不会提交 GC 完成态，而是进入 tombstone 重试。

## 用户体验

删除预览继续显示“引用数”，不冒充可删除文件数。CLI/New UI/TUI/Agent Tool 的完成信息展示
Artifact 删除、已缺失、保留共享和跳过风险的聚合计数，不暴露本机绝对路径。API 保持 204 完成、
202 安全重试、503 重试耗尽的既有协议。

## 验收证据

- 真实文件：Check 路径与 Evidence URI 别名去重后只删除一次。
- 共享引用：其他 Session 仍引用时保留；无存活引用时删除。
- 安全边界：普通项目文件、穿越、百分号穿越、符号链接、目录和工作区外文件均保留。
- 故障恢复：Artifact I/O 失败生成 `artifact_gc` tombstone，后续 worker 从 `records_committed`
  恢复并解决。
- 历史迁移：v4 的 `records_committed` 回填 pending，支持新失败阶段且原协调记录不丢失。
- Engine 真实场景：Session、Harness Run、运行时授权和物理 Artifact 在一次删除中完成协调。

## 明确未完成

HAR-06.5 负责 10k 级批次提交、空间/时间预算、周期调度、主动取消、吞吐指标和 retention policy
选取。本模块保证单请求删除的安全与可恢复性，不以长事务冒充通用后台 GC 平台。
