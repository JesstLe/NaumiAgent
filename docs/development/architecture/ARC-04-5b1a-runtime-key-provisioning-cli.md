# ARC-04.5b1a Runtime Key Provisioning CLI

## 1. 问题

ARC-04.5b1 提供了只读、resolve 和显式 provision API，但没有用户可执行入口。若 ARC-04.5c 直接让
embedded Agent 消费加密 Job Store，新安装会正确 fail closed，却只能依赖用户手工编写 Python 或环境
变量修复，无法形成产品闭环。

本切片只补密钥管理入口，不自动创建、不轮换、不导出密钥，也不改变 Agent 执行路径。

## 2. 命令

```bash
naumi runtime-key status
naumi runtime-key init
```

`status`：

- 只读系统 credential backend；
- missing 时退出码为 1，并给出精确 `init` 下一步；
- ready 时只显示 `runtime-payload-v1:<24 hex>` 非敏感 key ID；
- 不创建 key、不输出 Base64/hex key material。

`init`：

- 是唯一允许创建系统 Runtime payload key 的显式用户动作；
- 复用 `provision_runtime_payload_key()` 的进程内互斥和幂等语义；
- 已存在时返回同一 key，并明确显示“未执行轮换”；
- backend 失败返回简短中文错误和非零退出码，不打印 traceback、secret 或 backend 原始细节。

macOS 第一次执行 `init` 可能显示系统凭据授权，这是用户主动触发的操作。普通 `naumi` 启动、
`doctor`、配置加载和 Store Catalog 检查仍不会创建 Runtime key。

## 3. 自动化环境

CI/容器可以设置 canonical Base64 的 `NAUMI_RUNTIME_PAYLOAD_KEY`，不需要执行 `init`，也不会访问
系统 credential backend。`status` 会显示来源为环境变量，`init` 会明确跳过系统写入；两者都不会
打印该环境变量；secret 应由 CI secret manager 注入。

## 4. 验收

- missing status 只读、退出 1、下一步可复制；
- ready status 只显示 key ID；
- 环境变量 override 下 status/init 都不访问 credential backend；
- init 首次创建和已有 key 两条文案可区分；
- 重复 init 不声称轮换；
- 输出不包含 key bytes 的 hex；
- backend error 无 traceback；
- Typer help 明确列出 `init` 与 `status`；
- 只运行 Runtime key/credential/CLI 小模块，不访问真实 Keychain。

## 5. 未完成

- 不支持 rotate/export/import/recovery；这些必须先设计 old-key catalog 和 reencrypt 流程；
- New UI/TUI 内没有密钥管理页面，当前使用非交互 CLI 作为唯一显式管理入口；
- status 是用户主动的 credential read，操作系统是否显示授权由平台 credential backend 决定；
- ARC-04.5c 已让 embedded Agent durable dispatch 实际依赖该 key；缺 key 时在模型调用前给出本命令入口。
