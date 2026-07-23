# ARC-04.5b1 Runtime Payload Key and Envelope Authority

## 1. 为什么必须先做

ARC-04.5a 已让 embedded Agent 在模型调用前签发 request contract，但 durable Agent Job 若直接保存 raw
task/context，会把用户内容、消息、潜在凭据和工作区证据以明文写入 SQLite。仓库此前只有 Browser 私有的
环境变量 AES helper，没有通用 key identity、AAD、大小边界、canonical serialization 或系统密钥生命周期。

只保存 digest 也不成立：scheduler 重启后无法恢复 payload，不能安全派发。因此 ARC-04.5b 的最小前置不是
claim lease，而是可恢复且 fail-closed 的 authenticated payload envelope。

## 2. Runtime Payload Key Authority

`config.credentials` 新增独立账号 `runtime.payload_encryption_key.v1`：

- key 固定为随机 32 bytes，并以 canonical Base64 存入 OS credential backend；
- `load_runtime_payload_key()` 只读取，不隐式创建；
- `provision_runtime_payload_key()` 是显式、进程内互斥、幂等初始化；已有 key 永不静默轮换；
- `resolve_runtime_payload_key()` 优先消费显式 `NAUMI_RUNTIME_PAYLOAD_KEY`，便于 CI/容器受控注入；
- 环境变量缺失且系统 key 未 provision 时 fail closed，不回退到明文、API key、workspace 文件或固定 key；
- backend 读取由既有进程缓存复用，正常运行不会为每个 payload 反复访问 Keychain。

显式 provision 可能触发操作系统第一次凭据授权，这是唯一允许的 key 创建时机；导入配置、启动 embedded
Agent 或普通 Doctor 不会自动创建 key。跨进程首次创建必须由单一配置流程完成，不能让多个 Runtime 竞态生成。

## 3. AES-256-GCM Envelope

`RuntimePayloadKey` 只公开由 key SHA-256 前缀导出的 rotation identity，`repr` 不包含 key bytes。

`PayloadEnvelope` schema v1 包含：

- `aes-256-gcm`、key ID、12-byte random nonce；
- ciphertext（含 16-byte GCM tag）的 canonical Base64；
- plaintext 字节数，但不保存可用于低熵内容离线猜测的 plaintext digest；
- caller-provided AAD SHA-256；
- 覆盖全部 public envelope 字段的 SHA-256。

`seal_runtime_payload()` 只接受：

- 最大 16MB plaintext；
- 1..4096 bytes AAD；
- 精确 32-byte key 与精确 12-byte nonce。

`open_runtime_payload()` 在返回任何 bytes 前验证 key ID、AAD digest、GCM tag 和 plaintext 长度。
wrong key、wrong AAD 和 tag tamper 返回同一低敏错误，不暴露 cryptography 异常、key、plaintext 或 AAD。

Envelope SHA-256 用于检测意外结构损坏，不代替 GCM 认证；即使攻击者重算公开摘要，修改 ciphertext 仍会被
GCM tag 拒绝。

## 4. 聚焦验收

- 系统 backend 未 provision 时只读返回 missing，不自动写入；
- 显式 provision 只生成一次 32-byte key，重放返回相同 key；
- malformed/non-canonical Base64 和非 32-byte stored key fail closed；
- 环境注入完全绕过 backend；
- 相同 payload 的不同 nonce 产生不同 ciphertext；
- wrong key、wrong AAD、重算 envelope digest 后的 ciphertext tamper 都不能解密；
- serialized envelope 与 `repr` 不包含明文，key `repr` 不包含 key material；
- extra fields、错误 nonce、超大 payload 和 digest tamper 被拒绝；
- 只运行 credentials、payload envelope、security import 与架构所有权小模块。

## 5. 自我审视与未完成

- 本切片不保存任何 Agent Job，也不改变现有 embedded 委派路径；
- key rotation 目前只有 key ID 形状，没有 old-key catalog、reencrypt 或 revoke 流程；
- OS credential backend 的跨平台可用性仍需 Mac/Windows/Linux 打包矩阵验证；
- 环境变量注入适合 CI/容器 secret，不应写入 shell history 或项目配置；
- 没有跨进程 provision CAS；显式单一配置流程是当前硬约束；
- envelope 仍公开 ciphertext 长度；若未来任务需要隐藏长度，应在更高层引入有上限的 padding policy；
- backup/export 不包含 key，恢复到新设备必须单独迁移或重新 provision 并明确旧 payload 不可解密。

下一切片 `ARC-04.5b2 Durable Agent Job Authority` 才能使用 request SHA-256 作为 AAD，把 task/context 的
受控 JSON envelope 与 lifecycle/fencing 写入独立 Store。它不得保存明文 fallback。
