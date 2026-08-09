# ARC-07.5a Immutable Installed-Version Slots

## 目标

为闭源发行产物建立跨平台、不可变、可启动验证且可原子切换的本地版本槽。该切片是 updater 与 EVO-05.6b2
真实回滚执行器共享的最小基础设施，不实现下载、远端 channel、签名或数据迁移。

## Slot 安装

`ReleaseSlotStore.install()` 消费 ARC-07.1/2 已生成的 bundle 目录：

1. manifest 必须是受支持的 NaumiAgent v1 schema，最大 8 MiB、最多 100000 个文件；
2. 文件集合必须与 manifest 完全一致，逐文件复算 size/SHA-256；
3. symlink 必须留在 bundle 内，路径拒绝 absolute、`..`、反斜杠、NUL 和换行；
4. 二次执行 source-exposure audit，拒绝 Naumi 自身 `.py/.pyc/.js/.ts`、开发目录和 manifest；
5. 强制存在 target 对应的 `naumi[.exe]` launcher、`naumi-runtime[.exe]`、`naumi-ui[.exe]` 与配置示例；
6. 在同一 slots filesystem 内 staging、校验、rename、directory fsync，再把文件/目录改为只读；
7. Slot ID 由 manifest digest 导出，SQLite `BEGIN IMMEDIATE` 保证并发幂等登记。

POSIX slot 文件与目录均移除写权限；Windows 文件设置只读属性，目录 ACL 不作为安全权威，激活前仍必须逐文件复算
manifest，以检测新增、删除或替换。

安装不覆盖、删除或修改旧 slot，也不改变 active pointer。

## Bootability Receipt

激活仅接受当前主机 target。`verify_bootable()` 在重新验证完整 bundle 和只读权限后，真实执行 exact backend
`--version`，限制 20 秒与 64 KiB 输出，并要求 manifest version 是独立 token，而非模糊子串。Receipt 保存最多
4096 字符的 UTF-8 version output，并绑定 slot/manifest/binary digest、参数、output digest、duration 与检查时间；失败
不产生 activation authority。

## Atomic Active Pointer

Active Pointer 和 append-only event 在同一 SQLite FULL-synchronous 事务中更新：

- generation 连续增长并链接 previous pointer digest；
- 每次读取和切换都重放完整 `1..N` event chain，并要求 singleton pointer 等于 history tail；
- `get_activation_event(generation)` 只在完整重放并验证 event chain 后返回指定 immutable generation，
  供部署/回滚崩溃对账读取已经不再是 tail 的历史切换；
- activation 前重新验证目标 slot、Boot Receipt 的 binary digest，以及当前旧 slot 仍存在且不可写；
- rollback 只能选择 current pointer 的 exact previous slot，并用 expected pointer digest 防止并发误回滚；
- 切换永不删除旧 slot，因此进程崩溃时数据库只会呈现完整旧 generation 或完整新 generation。

SQLite pointer 是权威，避免 Windows 不可靠 symlink/junction 语义；
[ARC-07.5b](ARC-07-5b-stable-slot-launcher.md) 已让稳定 launcher 消费该 pointer。

## 验收结果

- POSIX 真实脚本完成 install → boot → activate v1 → activate v2 → rollback v1；
- 两个 slot 均保留，generation 为 1/2/3，rollback previous 指向 v2；
- 完整链验证后可精确读取 generation 1/2/3，越过 tail 返回 missing，链中缺口失败关闭；
- 八线程并发安装同一 bundle 只形成一个 slot；
- 未 boot、版本输出不匹配、文件篡改、源码泄漏全部失败关闭；
- target 由 macOS/Linux/Windows 与 arm64/x64 机械映射；
- 只运行相关小模块测试，未运行全量测试。

## 当前边界与下一步

- ARC-07.4 仍需为 manifest/build provenance 增加发行签名；本切片的 SHA-256 只证明本地一致性；
- ARC-07.5b 已让稳定 launcher 从 SQLite active pointer 启动 exact slot，并失败关闭损坏状态；
- ARC-07.6 仍需配置/数据 snapshot 与 migration compatibility；
- EVO-05.6b2 仍须把 exact rollback authority 与本启动链连接，并验证回滚后的新 Launch Resolution。
