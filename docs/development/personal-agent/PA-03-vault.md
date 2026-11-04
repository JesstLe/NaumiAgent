# PA-03 独立连接凭据加密

## 职责与边界

`personal/connection_vault.py` 负责连接 token、OAuth PKCE 和 client 数据的 AES-256-GCM 信封加密。SQLite 事务、revision CAS、授权状态、单写 lease 和信封持久化由 ConnectionStore 负责。组件不读取现有 config、模型 API key、`.env`，不启动 provider 或 Engine，不提供明文文件后备。

系统凭据库只保存每个 owner/slot 一把随机 256-bit AES key。token bundle 留在内存或以密文信封存入 SQLite，不直接写入 Windows 单条 credential，从而不受其小容量限制。固定专用 service 为 `NaumiAgent.PersonalConnections.v1`，account 为 `owner_key + '/' + slot`。

## 接口合同

```python
vault = ConnectionVault(backend=None)
# The caller holds its persistent, owner/slot-bound write lease here.
vault.provision(owner_key=owner_key, slot=slot)
envelope = vault.seal(owner_key=owner_key, slot=slot, purpose="oauth-token", value=bundle)
bundle = vault.open(owner_key=owner_key, slot=slot, purpose="oauth-token", envelope=envelope)
status = vault.delete(owner_key=owner_key, slot=slot)
```

- 默认 backend 显式选择原生系统库：Windows `WinVaultKeyring`、macOS `Keyring`、Linux `SecretService`。不使用可配置 keyring 默认链，不会落到第三方文件 backend；原生库缺失/不可用明确失败。测试 backend 只需实现 `get_password`、`set_password`、`delete_password`。
- owner 必须为 64 个小写 hex。slot 只允许 `conn_<32hex>`、`oauth_<32hex>`、`client_<64hex>`。
- purpose 为 1–64 个 ASCII 字母/数字/`_.:-`，必须以字母开头。建议固定使用 `oauth-token`、`oauth-pkce`、`oauth-client`，不同用途不得复用 purpose。
- `provision()` 是唯一创建密钥的接口。调用者必须持久化 lease 保证跨实例/进程单写；组件的实例 RLock 仅保护同实例线程。已有密钥只验证，不覆盖；set 后读回不一致或失败不触发覆盖、删除或“自动恢复”。
- 每次 `seal/open` 都实际从 backend 读 key，无长期 key 缓存。缺 key 明确失败，不自动 provision。
- `delete()` 返回精确状态：`{'status':'confirmed'}` 表示 delete 成功且后续读取确认缺失；`missing` 表示删除前已确认不存在；`error` 表示 OS 调用失败或删除后仍存在/不可确认。调用失败不伪造删除成功。
- 调用者需用相同持久化 lease 序列化 provision/delete 与连接身份切换。seal/open 与正在发生的 delete 竞争时，已取得 key 的那次调用可能完成；之后的读取会重新确认缺 key。跨进程强制撤销不是 AEAD 操作本身的合同。

## 加密与输入合同

信封含严格字段 `v=1`、`alg=A256GCM`、12-byte 随机 nonce、ciphertext。Base64 使用严格校验与 canonical re-encode 校验，拒绝额外字段、重复 JSON key、错误类型、未知版本、短 nonce 和短 tag。

AAD 绑定版本、算法、owner、slot、purpose；即使复制同一把 key 到另一 owner/slot，旧信封也不能通过身份验证。AES-GCM 的认证 tag 为 128-bit。nonce 来自系统 CSPRNG，每次 seal 独立产生；随机 nonce 不等同于数据库级永久唯一性计数器，调用方应使用正常 token-bundle 工作负载，不用它做海量流式加密。

明文必须为严格 JSON object，最多 1 MiB UTF-8、32 层深度、100,000 个节点；拒绝非有限数、tuple/set、非字符串 key、循环、无效 Unicode。序列化通过有限累积的 iterencode 提前拒绝超限 aggregate，不先拼接巨型明文。密文信封有独立 ASCII/容量上限。解密后再次验证 JSON 与边界，返回值与输入/旧结果脱离。

## 错误与脱敏

- `invalid_input`：身份、purpose、JSON、信封或容量不正确。
- `credential_missing`：系统 key 已缺失。
- `credential_error`：系统 keyring 调用失败、key 编码损坏、provision 读回不一致。
- `vault_corrupt`：AAD、密文认证或已解密内容验证失败。

所有错误使用固定中文文案，不加入原始 exception、token、key、plaintext 或 backend repr。backend 异常在 except 结束后转换，避免保留用户可见异常链。组件不日志记录密文或秘密，不在 repr 中展示 backend。

## 验证记录

本域源码、单测和文档完成后集中运行 lint/import/pytest。下列记录分为本域真实 AES-GCM + fixture backend 验证和隔离原生 Windows 凭据库验证；不等同于上层 OAuth/ConnectionStore 端到端验收。

### 本域集中验证

- `python -m ruff check src/naumi_agent/personal/connection_vault.py tests/unit/test_personal_connection_vault.py`：`All checks passed!`，exit 0。
- import smoke：`VAULT_IMPORT_OK` 与 `ConnectionVault()`，exit 0；构造没有读取任何 credential。
- 首轮整域测试 `74 passed in 0.25s`；自审收紧 default native backend 与 bounded streaming JSON 后 `76 passed in 0.29s`，两轮 exit 0。
- 最终命令：`.\.venv\Scripts\python.exe -m pytest tests/unit/test_personal_connection_vault.py -q --tb=short`；补充 canonical-base64 与并发快照边界后实际输出 `78 passed in 0.29s`，exit 0。最终 lint 输出 `All checks passed!`，exit 0。
- 单测覆盖：3类slot、owner/slot/purpose替换、nonce/ciphertext篡改、未知版本/字段/重复JSON key、无效base64/Unicode、payload/envelope容量、cycles/depth/nodes、512次nonce采样无重复、80个并发独立实例在外部lease下只建一把key、系统库缺失/失败、provision读回冲突、delete确认与失败、已认证但内容非法的明文。

### 原生 Windows 凭据库隔离实测

实测只调用本轮随机 account，没有枚举系统 credential，也没有读取用户既有 key。backend 为 `keyring.backends.Windows.WinVaultKeyring`；service 为本组件专用 `NaumiAgent.PersonalConnections.v1`。

```json
{"backend":"keyring.backends.Windows.WinVaultKeyring","delete":{"status":"confirmed"},"deleted_readback_missing":true,"envelope_bytes":26890,"existing_user_entries_read":0,"namespace":"pa03-vault-isolated-5513bb90324b42d48b53d77dd35731b7","os_key_bytes":32,"owner_key":"bdb42d3b773afde9a4b39495dcd43f030aa80d61e81cc3debfb6df6606613087","plaintext_utf8_bytes":20102,"platform":"win32","roundtrip":"PASS","service":"NaumiAgent.PersonalConnections.v1","slot":"conn_66c62779ab17460bb69cedb37bede9f4"}
```

真实调用序列：随机owner/slot首次get确认缺失 → provision → 读取同一随机account核验key长度32bytes → seal/open 20,102bytes合成bundle一致 → finally delete → 同一随机account再次get为None。进程 exit 0。秘密值未输出、未落明文文件，临时key删除已确认。

### 当前完成

- 独立系统密钥、AEAD 信封、owner/slot/purpose/version 绑定。
- 严格输入、有限容量、nonce 随机化、每次读系统 key。
- 显式 provision 与删除确认、OS 故障脱敏、不降明文文件。
- 同请求域的单写 lease 合同与并发往返测试。

### 上层仍负责

- SQLite schema、密文与 metadata 原子 CAS、持久化 lease、OAuth 状态机。
- 真实 OAuth HTTP 往返、用户授权与断开连接的 UI 闭环。
- 业务 scope 规则、token 刷新与服务器撤销回执。
