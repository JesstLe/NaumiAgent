# ARC-05.3a SQLite 预迁移备份与 Manifest

## 1. 交付目标

在 ARC-05.2a 单 Store Migration Runner 与任何生产 Store 接线之前，建立一个可独立计划、创建和复核的
SQLite 预迁移备份 authority。该切片解决空间预检、WAL 一致性快照、原子发布、私有权限、canonical
manifest/digest 和篡改检测，使后续迁移编排能够引用一份明确的旧版本证据。

本切片不自动迁移任何 Store，不接入启动流程，不执行 restore，不跨 Store 编排，也不提供“备份成功即
可安全升级”的宽泛结论。ARC-05.2b 必须把备份 receipt、迁移计划和持久 journal 绑定后才能 apply。

## 2. 生产落点

- 实现：`src/naumi_agent/persistence/backups.py`；
- 聚焦测试：`tests/unit/test_sqlite_backup_manager.py`；
- 输入：ARC-05.1 `StoreDefinition`，且仅接受 `StorageKind.SQLITE` 与
  `VersionStrategy.SQLITE_USER_VERSION`；
- 输出：`SQLiteBackupPlan`、`SQLiteBackupManifest`、`SQLiteBackupReceipt`；
- manifest schema：`BACKUP_MANIFEST_SCHEMA_VERSION = 1`。

Store ID 必须能安全映射为相对目录名。源 Store 不存在、不是普通文件、是符号链接、损坏、未声明目标
版本或高于当前支持版本时失败关闭，且不会创建源文件或备份目录。

## 3. Read-only Plan

`SQLiteBackupManager.plan()` 只读取源 SQLite 和目标文件系统事实：

- 当前/目标 `user_version`；
- SQLite `page_count * page_size` 逻辑大小与源主文件大小；
- 最近已存在目标父目录的可用空间；
- 已存在备份根目录的 POSIX mode 与是否私有；
- 至少 `max(main file, logical size) + max(1 MiB, 10%)` 的空间门。

Plan 不创建缺失备份目录。空间不足以 `sufficient_space=false` 明确呈现，create 再以 typed
`BackupSpaceError` 阻断。空间探测异常、布尔/负数等无效结果也失败关闭，不把未知容量当作足够。

普通 rollback-journal SQLite 的 plan 不改变主文件 bytes、size、mtime 或 digest。WAL 模式下 SQLite
只读连接可能通过 mmap 更新易失的 `-shm` 协调区；持久 `.db` 与 `-wal` 不改变，`-shm` 不作为 durable
备份内容或“逐字节不变”证据。不能为了保持 `-shm` 不变而使用忽略 WAL 的 `immutable=1`，否则会漏掉
已提交数据。

## 4. 一致性快照与原子发布

```text
catalogued source SQLite (mode=ro, query_only)
        |
        v
SQLite online backup API -> private staging/store.sqlite3
        |
        +--> PRAGMA quick_check
        +--> user_version/page_count/page_size
        +--> SHA-256 + canonical manifest.json
        |
        v
fsync files + staging directory
        |
        v
atomic rename staging directory -> <store_id>/<backup_id>
        |
        v
independent verify() -> SQLiteBackupReceipt
```

- backup ID 为 `arc5b_` 加 24 位随机 hex，不以路径或时间充当身份；
- staging 与最终目录位于同一 Store backup root，目录级 rename 避免“数据库可见但 manifest 缺失”；
- `store.sqlite3` 与 `manifest.json` 使用排他创建，发布前 fsync；
- 发布、SQLite backup、manifest 写入或最终 verify 失败时清理本次拥有的 staging/final 目录；
- 同一 backup ID 并发时只有一个获胜，失败竞争者不得删除获胜者的 staging；
- 不覆盖既有 backup ID，也不把部分结果伪装成 receipt。

## 5. Manifest 与隐私

Manifest 使用 UTF-8、排序 key、无多余空白、尾随换行的 canonical JSON，固定字段为：

- schema/backup/store identity 与 UTC `created_at`；
- `source_path_sha256`，不保存绝对源路径明文；
- source/target schema version；
- 固定相对文件名 `store.sqlite3`；
- backup SHA-256、字节数、page count 和 page size。

Receipt 额外返回 manifest SHA-256 和本地路径，供同进程后续编排引用。Manifest 不包含 SQL、表内容、
secret、聊天、reasoning 或异常原文。错误消息只暴露稳定 Store ID 和恢复动作，私有底层异常保留在
exception cause。

## 6. 权限与路径边界

- POSIX 新建 backup root、Store 目录和 backup 目录为 `0700`；SQLite 与 manifest 为 `0600`；
- 已存在根目录若向 group/other 开放，Plan 显示 `destination_private=false`，create 拒绝且不自动 chmod；
- source、backup root、published directory、SQLite 文件和 manifest 的最终组件不得为符号链接；
- manifest 的 backup 文件名固定，未知字段、路径越界、重复身份和非 canonical JSON 均拒绝；
- Windows 当前只跳过 POSIX mode 判断，等价 ACL 创建/验证仍属于 ARC-05.3b/ARC-07，不能宣称完成。

## 7. 独立 Verify

`verify()` 不依赖 create 的内存状态，重新执行：

1. published/store/backup identity 绑定；
2. manifest 大小上限 64 KiB、精确字段集、类型、版本关系、UTC canonical 时间与 digest 格式；
3. POSIX directory/file mode；
4. backup size 与 SHA-256；
5. SQLite `quick_check`、`user_version`、`page_count` 与 `page_size`。

修改 backup bytes、manifest 字段、权限、路径或 SQLite 元数据都会使验证失败。SHA-256 和私有目录提供
本机完整性检测，但本切片没有签名或外部信任锚；离线攻击者若能同时改写文件和 manifest，真实性仍需
ARC-07/ARC-08 的签名与审计链。

## 8. 验收证据

| 场景 | 权威预期 |
| --- | --- |
| dry-run + 缺失目标目录 | 不创建目录，源主文件 bytes/size/mtime/digest 不变 |
| WAL 内有已提交未 checkpoint 数据 | backup 包含该数据，源 `.db`/`-wal` 不变 |
| 空间不足 | Plan 明确不足，create 不创建任何输出 |
| 已存在 `0755` backup root | Plan 标记不私有，create 拒绝且不修改 mode |
| publish 或最终 verify 故障 | 不保留可见的半备份或 staging |
| backup/manifest 篡改 | independent verify 失败关闭 |
| 缺失、损坏、高版本、符号链接、非 SQLite | 不创建输出并返回有界中文错误 |
| 同 ID 两线程并发 | 一份 verified receipt、一个 typed loser、无残留 staging |
| ARC-05.2a 真实迁移 | current Store 升到 v2，backup 仍为可验证 v1 且结构未变 |

本切片只运行 `test_sqlite_backup_manager.py`、`test_migration_runner.py` 相关节点和 touched-file Ruff/
compile，不运行全量测试。

## 9. 未完成边界与下一步

- ARC-05.2b：将 Plan、Backup Receipt、apply attempt 和 crash 判定写入持久 journal；
- ARC-05.3b：默认 backup root policy、Windows ACL、配额、retention 与多 Store batch preflight；
- ARC-05.4：领域级 foreign key/orphan/JSON/artifact 完整性检查；本切片只有 SQLite quick check；
- ARC-05.5：显式 restore、恢复前二次快照、只读安全模式和人工修复入口；
- ARC-05.6/5.7：跨 Store saga 与 retention/GC；
- ARC-07/08：签名、远端/离线副本、灾难恢复演练和长期可观测性。

因此 ARC-05 保持 `partial (5.1, 5.2a, 5.3a)`，Migration Runner 仍不得无提示接管生产启动路径。
