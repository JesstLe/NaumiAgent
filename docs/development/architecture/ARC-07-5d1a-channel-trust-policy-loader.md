# ARC-07.5d1a Channel Trust Policy Loader

## 状态

已实现。

## 目标

为安装级 `trusted-channels.json` 提供正式、跨平台、失败关闭的运行时加载入口，使 Production Engine 能把
Release Channel Catalog Store 绑定到进程外公开信任根，而不是由测试代码直接注入 Pydantic 对象。

该文件只包含 Channel signer 的公开密钥、generation、channel scope、有效/撤销窗口和固定 archive origins；
不保存私钥或 API secret。

## 加载边界

`load_release_channel_trust_policy(path)`：

1. 对路径执行 `lstat`，拒绝目录、设备、FIFO 和符号链接；
2. 文件必须在 `1..512 KiB`；
3. 以只读 descriptor 打开；支持的平台使用 `O_NOFOLLOW`；
4. 对比打开前后的 device/inode，阻止路径在检查与打开间被替换；
5. 从同一 descriptor 有界读取，并对比读取前后的 size/mtime/ctime；
6. 用 strict `ReleaseChannelTrustPolicyDocument` 解析，拒绝未知字段、非法 key、重复 identity、非 canonical origin
   和 digest 不一致。

任何读取、identity、大小或模型验证问题都会产生稳定的 `ReleaseChannelCatalogError.code`，不会退回空信任、
环境变量 signer 或“信任全部”。

## 安装布局

Production Engine 的目标布局为：

```text
<release-root>/
  trust/
    trusted-builders.json
    trusted-channels.json
  state/
    release-channel-catalog.db
```

两个 trust root 相互独立：Channel policy 允许发布 channel，Build policy 证明 archive/source provenance。
Target Baseline Resolution 必须同时通过二者。

## 验收证据

- 真实 Ed25519 Channel signer 生成的 policy 可 round-trip 加载；
- `{}` 等不完整 JSON 被 stable error code 拒绝；
- 空文件与超过 512 KiB 的文件在解析前拒绝；
- 缺失文件被稳定的 unreadable code 拒绝；
- POSIX symlink 被拒绝；
- Release package public export 已包含 loader；
- 相关小模块测试、Ruff、py_compile 与 diff check 通过；未运行全量测试。

## 后续

`HAR-09.6c2a2b / ARC-07.5h` 将在 Engine 中从上述安装布局组合 Channel Catalog Store，并用
HAR-09.6c2a2a Placement 的 exact target 解析目标平台 baseline。
