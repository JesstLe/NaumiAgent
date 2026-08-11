# ARC-07.5c1 Managed Installation Key Provisioning

## 目标

补齐 ARC-07.5c 留下的 installation private key 生产缺口，使每台 managed installation 能真实持有与
`ReleaseManagedInstallationCredential` 公钥对应的 Ed25519 私钥，而不是由测试 fixture 临时生成。私钥只写入操作系统
凭据库，不进入 `.naumi`、release SQLite、日志、Tool 回执或公开 metadata。

本切片只负责显式初始化、公开身份检查和固定协议域签名；不实现远端 Registry enrollment、Credential renewal/revocation、
密钥轮换、远端 daemon transport 或 rollout authority。

## 存储与生命周期

`ReleaseInstallationKeyService` 使用安装根目录作为本地 ownership 边界：

- 公开 handle：`<release-root>/trust/installation-key.json`，权限固定为 `0600`；
- 跨进程锁：`<release-root>/state/installation-key.lock`；
- 私钥：系统 `keyring` 的 `NaumiAgent` service；account 只含 channel、release-root SHA 前缀与 public-key SHA 前缀；
- metadata 只保存 release-root digest，不保存 raw path、用户名、machine ID 或 private seed；
- `provision` 必须显式触发；`AgentEngine` 构造和 `inspect` 均不会读取 keyring；
- 同一 install root/channel 的并发初始化由 POSIX `flock` 或 Windows `msvcrt.locking` 收敛为一个 key；
- keyring 写入后 metadata 原子写入失败会尽力删除未引用 secret，且永不把 seed 放入异常文案。

macOS、Linux、Windows 的凭据后端由既有 `keyring>=25.6` 依赖选择 Keychain、Secret Service 或 Windows Credential
Locker。后端缺失或锁定时显式失败，不降级为明文文件。第一次 provision/签名可能由操作系统要求解锁；同一进程成功加载后
缓存私钥对象，避免每次签名重复访问钥匙串。普通启动不触发这类提示。

## 公开 Handle

`ReleaseInstallationKeyHandle` 内容寻址并冻结：

- channel、public key 与 SHA-256；
- release root SHA-256 与 deterministic keyring account；
- `private_key_stored_in_os_keyring=true`；
- `private_key_written_to_metadata=false`；
- `private_key_returned_to_caller=false`；
- raw machine/user/path collection 均为 false；
- generation time 与 policy version。

读取拒绝 symlink、非 regular file、超过 64 KiB、group/world writable、结构/摘要漂移。broken symlink 也按不安全 metadata
失败关闭，而不是误报为“尚未初始化”。

## 固定协议域签名

本模块不提供“签任意字符串”的 Agent Tool。内部 `sign_remote_readiness_probe()` 只接受
`naumi.release.stable-remote-readiness-probe.v1` domain，并要求：

1. exact Population Credential 的 channel/public key/public-key SHA 与本地 handle 一致；
2. payload 为 1..64 KiB bytes；
3. signature 输入固定为 canonical domain、key、Credential、member、payload size/SHA 与 signed time binding；
4. 回执绑定同一组认证字段、signature SHA 与时间，字段改写会破坏签名；
5. verifier 使用 Credential public key 重验 Ed25519 signature。

该签名只证明当前安装私钥对 exact payload 负责，不自动形成 readiness、execution、rollout 或 promotion authority。

## 双通道

- Agent Tool：`evolution_installation_key`；
- `/evolution installation-key provision [channel]`：显式初始化；
- `/evolution installation-key inspect`：只读公开 metadata，不访问 keyring；
- CLI、Textual TUI fallback 与 New UI 走共享 Slash/Tool；
- moderate/bypass 均不增加第二次确认，lockdown 阻断写 Tool；机械 keyring/metadata gates 不可绕过。

## 验收证据

- 八个独立 Service 并发 provision 只调用一次 key factory、写一份 keyring secret 和一个 handle；
- 真实 Ed25519 seed、Registry Credential、固定 domain payload 完成签名和公钥验证；
- payload 或 Credential 漂移失败关闭，重复签名在固定时钟下确定性一致；
- metadata 不含 raw release path 或 private seed，权限为 `0600`；
- keyring unavailable 的错误脱敏且不留下 public metadata；
- digest 篡改、权限过宽、regular-file 违规与 broken symlink 均失败关闭；
- 默认 Engine 构造不访问 keyring，真实 Slash provision/inspect 共享同一 Tool Service；
- 只运行本模块、权限、Tool 注册、New UI route、Ruff、compile/import 小检查，不运行全量测试。

## 未完成边界与下一步

- 自动 Registry enrollment/renewal/revocation API 尚未实现，当前需把 handle public key 交给外部 Registry；
- rotation、retirement、backup/recovery 与企业 HSM/TPM non-exportable key 尚未实现；
- 三平台真实 OS credential backend 的发布 runner 验收仍需各平台 CI，单元测试使用无落盘的 backend adapter，不能冒充平台认证；
- 下一切片 EVO-05.5f5x3b 应在远端安装上调用受限 Release Store probe，再使用本模块固定 domain 签名其 fresh source
  digest；Control Plane 验证后才可把 x3a 的 `source_runtime_revalidated=false` 升级为独立、短期的 binary rollback
  readiness authority。
