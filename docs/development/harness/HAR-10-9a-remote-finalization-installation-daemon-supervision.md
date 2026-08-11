# HAR-10.9a Remote Finalization Installation Daemon Supervision

## 1. 目标与依赖裁决

EVO-05.5f5x3g 已交付 Control Plane → installation 的 mTLS Delivery endpoint，x3h 已交付
installation-local writer、Result 签名与 durable outbox，x3i 已交付 installation → Control Plane 的
mTLS Result transport。但这些 component 仍由交互式 Engine 分别启动：入站 server 没有 owner，Result
Worker 会跟随 New UI/TUI 进程退出，也没有跨进程可发现的健康事实。

本切片把它们组合成一个可由 OS service manager 托管的前台 daemon：

```text
exact installation member
  + workspace-scoped Harness RunLease owner/epoch
  + x3g inbound mTLS server
  + x3h durable Result Worker using x3i transport
  + typed Runtime heartbeat
  + atomic local discovery descriptor
  -> one recoverable installation service authority
```

本切片不实现 Population Receipt aggregation，不增加新的 Release 执行权限，不实现 DNS/Consul/Kubernetes
service discovery，也不提交 launchd/systemd/Windows Service 安装脚本。它提供跨平台前台进程和机械
owner/fail-closed 语义，发布工程可在后续切片把同一命令接入各平台服务管理器。

## 2. 单 owner 与 crash residue 接管

每个 installation member 使用稳定的 Run ID：

```text
stable-finalization-installation-<sha256(member-id)[:24]>
```

daemon 启动前必须在 Harness Store 以 `run_kind=runtime` 获取该 RunLease。live foreign owner 存在时，
新进程只进入 `standby`，不得启动端口、Worker、heartbeat 或写 discovery。released/expired lease 可由新
instance 取得更高 epoch；因此 OS service manager 在原进程崩溃后重启时，不会复用旧 authority。

lease renewal 间隔不得超过 lease 的三分之一；lease 总长度必须覆盖 Result Worker drain 加两个续租
周期。续租返回 fenced、Store 异常或 heartbeat 持久化失败时，daemon 触发同一失败关闭序列：

1. 撤销当前 instance 的 discovery；
2. 停止 x3g admission，不再接收新 Delivery；
3. drain/cancel x3h Worker，保留 durable claim/outbox 供下一 epoch 恢复；
4. 尝试写 failed heartbeat；
5. 释放仍属于自己的 lease；若 authority 已丢失则绝不改写新 owner。

heartbeat、lease 和 discovery 都绑定 exact `instance_id + epoch`。旧 instance 删除 descriptor 时先读取并
比较 instance；即使新 owner 已原子覆盖同一路径，旧 owner 也不能删除新 descriptor。

## 3. 启动与停止事务边界

启动顺序固定为：

1. 获取 RunLease；
2. 启动 x3g mTLS server 并获得实际 bound port；
3. 保存 `starting → running` typed heartbeat；
4. 启动 x3h Result Worker；
5. 原子发布 discovery descriptor；
6. 启动 lease renew 与 failure monitor。

任一步失败会反向回滚已启动 component、写失败 heartbeat（若 heartbeat 已建立）、删除 exact instance
descriptor 并释放 lease。daemon 不会留下“descriptor 已发布但 endpoint 不存在”的成功外观。

优雅停止顺序是 `draining heartbeat → remove discovery → stop admission → Worker drain → terminal
heartbeat → lease release`。terminal heartbeat 在仍持有 lease 时提交，避免新 epoch 已启动后旧 epoch
补写 stopped 覆盖新健康事实。

## 4. 动态 installation authority

x3g 的原本 local adapter 接受构造时固定 credential。daemon 增加
`ResolvingStableRemoteFinalizationInstallationTransport`，但没有放宽权限：

- daemon 配置固定一个 `installation_member_id`；
- package member 必须与该 ID 完全一致；
- credential 从 current Population Snapshot 动态解析，随后必须再次匹配同一 member；
- target journal、installation key 与 release root 必须同源；
- journal 继续重验 Trust Policy、Grant、credential、package 与 installation signature；
- bypass 不能绕过 member、lease、证书或 journal 机械校验。

动态解析只解决证书/Population Snapshot 轮换后的长期运行，不允许一个 daemon 接收多个 member。

## 5. 原子 service discovery

descriptor 固定写入 Runtime data directory：

```text
stable-finalization-installation/endpoint.json
```

它包含 schema/policy、member ID、HTTPS endpoint、服务端叶证书 SHA-256 pin、instance、lease epoch、PID、
时间戳和低敏 Worker 聚合计数。它不包含私钥/CA 路径、Control Plane 证书 identity、owner ID、Delivery ID、
Result/Receipt body、workspace 内容或错误 traceback。

安全边界：

- parent directory 在 POSIX 强制 `0700`，descriptor 强制 `0600`；Windows 权限由安装 ACL 负责；
- 拒绝 descriptor/parent symlink、非普通文件和超过 64 KiB 的内容；
- canonical JSON + SHA-256 覆盖除摘要字段外的完整 schema；未知、缺失、篡改字段全部拒绝；
- publish 与 exact-instance remove 共享 crash-safe OS advisory lock；旧 owner 的 read/unlink 临界区内，新 owner
  不能覆盖 descriptor，避免 TOCTOU 删除新 instance；
- 同目录随机临时文件使用 exclusive create，写入后 fsync、`os.replace` 并 fsync parent；
- `advertise_host` 只接受 ASCII hostname 或明确 IP，拒绝 unspecified address；endpoint 固定为 x3g HTTPS
  path，实际动态端口来自已启动 server；
- descriptor 是本机 bootstrap/discovery 事实，不替代 Control Plane 的 CA、hostname 和同连接 leaf pin
  验证，也不授予网络权限。

每次续租刷新 descriptor 的低敏 Worker 摘要。Agent Tool/Slash 不读取当前 UI Engine 的占位对象，而是
联合读取 descriptor 与 Harness heartbeat，重验 instance/epoch 后给出 `healthy / stale / offline /
identity_mismatch / invalid`。因此独立 daemon 的状态能在 New UI 与 Textual TUI fallback 中同源显示。

## 6. Runtime ownership 与命令入口

配置节点：

```yaml
harness:
  stable_remote_finalization_result_return:
    enabled: true
    shutdown_drain_seconds: 25
  stable_remote_finalization_result_http_transport:
    enabled: true
    # x3i Control Plane endpoint + installation client certificate
  stable_remote_finalization_installation_daemon:
    enabled: true
    installation_member_id: relpopmember_...
    bind_host: 0.0.0.0
    advertise_host: installation-01.example
    port: 8443
    server_certificate_path: /secure/installation-server.pem
    server_private_key_path: /secure/installation-server.key
    control_plane_ca_path: /secure/control-plane-ca.pem
    authorized_control_plane_certificate_sha256:
      - <current-control-plane-client-leaf-sha256>
      - <next-control-plane-client-leaf-sha256>
```

配置任一 daemon 身份/证书字段却未显式 enabled、字段不完整、member/pin 无效、heartbeat/renew/lease
边界不安全，或未同时启用 x3h Worker 与 x3i Result HTTP，均失败关闭。私钥仍复用 x3g 的 regular-file、
POSIX no-group/world-read 校验。

前台启动：

```bash
naumi stable-finalization-daemon --config /path/to/config.yaml
```

命令不读取模型 API key、不启动 New UI，也不隐式 fork/detach；服务管理器应直接监督此前台 PID。live owner
存在时退出为临时失败，而不是假装启动成功；运行期间 lease/heartbeat 失效完成 fail-closed 后，前台命令也会
退出为临时失败，使 service manager 能执行有界重启。交互式 Engine 在 daemon 配置启用后不再自动启动自己的
Result Worker，避免两个进程争抢同一职责；daemon 未启用时保留原内嵌兼容路径。

共享只读入口：

```text
/evolution stable-remote-finalization inspect-installation-daemon
Agent Tool: evolution_stable_remote_finalization(action="inspect-installation-daemon")
```

两者复用同一 inspect 后端；输出只包含 member hash、endpoint、epoch、heartbeat health 和聚合计数。

## 7. 跨平台边界

daemon core 使用 `asyncio`、线程化标准库 HTTPS server、SQLite RunLease/heartbeat、`pathlib` 和原子
`os.replace`，不依赖 fork、Unix socket、shell 或 POSIX signal。CLI 以前台 asyncio 任务运行，Ctrl-C/
进程取消进入 Engine shutdown；这给 macOS、Linux 和 Windows 提供同一进程合同。

当前真实 mTLS、权限位、fsync 与 executable Release Slot 证据来自 macOS。以下仍属于发布切片：

- launchd plist、systemd unit、Windows Service 包装和安装/卸载/升级流程；
- Windows ACL 与原子 replace 的真实 CI，Linux systemd sandbox/Capabilities；
- 三平台 crash-loop、网络 flap、磁盘满、证书 ACL 和 24 小时 soak；
- DNS/Kubernetes/跨主机 Control Plane discovery 与证书热重载/撤销分发。

因此本切片证明“同一 Python daemon contract 可被三平台管理”，不宣称三平台生产发布已经验收。

## 8. 验收证据

- [x] 真实 Control Plane Delivery Worker → x3g mTLS → target journal → x3h writer/sign → x3i mTLS →
  Control Plane Receipt 全链路；
- [x] daemon 运行时 descriptor、server leaf pin、typed heartbeat 与 Worker counters 可联合检查；
- [x] 两个 daemon 竞争同一 member，只有一个启动 component；graceful stop 后新 owner 获得更高 epoch；
- [x] 模拟 crash 遗留 expired lease 后，新 instance 以更高 epoch 启动；
- [x] 旧 owner 丢失 lease 后删除自己的 discovery、停止 endpoint/Worker，并且不能删除新 instance descriptor；
- [x] 停止后旧 endpoint 无法继续接受 Delivery；
- [x] descriptor 摘要篡改、stale-instance removal、unspecified advertise address 与危险 timing 失败关闭；
- [x] descriptor replacement 与旧 owner exact-remove 并发串行化，parent/lock symlink 失败关闭；
- [x] Runtime composition 构建 factory/daemon 但不启动后台工作；
- [x] New UI/TUI 通过共享 Agent Tool/Slash 读取跨进程 durable inspection；
- [x] foreground CLI 的 standby 路径保证 Engine shutdown；
- [x] foreground CLI 等待 daemon terminal 信号，运行期失败完成关闭后以失败退出；
- [x] 相关 daemon/x3g/x3h/x3i/Runtime/Config/Tool/CLI 小模块测试、Ruff、compile、lazy exports 与文档治理通过；
- [x] 按用户要求未运行全量测试。

## 9. 自我审视与下一步

本切片完成的是 installation daemon 的可恢复运行边界，不是 fleet rollout completion：

- daemon 依靠外部 service manager 负责进程重启，本身不 fork、后台化或安装 OS service；
- descriptor 是单机文件 discovery，不是跨主机注册中心；Control Plane 自动发现/推送仍未交付；
- 没有 PID/create-time process witness 或远程 kill；live process 的 lease 失效会在有界 renew/heartbeat 周期内
  自停，但不宣称瞬时全局 leader election；
- 证书 current/next 在启动时加载，尚无热重载、CRL/OCSP/SPIFFE 或撤销 push；
- endpoint rate limit 仍为单进程，不是多副本共享 authority；
- Result dead-letter review/requeue/abandon/retention 尚未交付；
- Windows/Linux 真实 service/ACL/soak 证据尚未形成。

[EVO-05.5f5x3j Population Finalization Receipt Aggregation Authority](../self-evolution/EVO-05-5f5x3j-population-finalization-receipt-aggregation.md)
已继续聚合 exact active Population Snapshot 中逐 member 的 current Receipt，并动态撤销 stale/missing/conflict authority；
daemon 健康、单 member 成功或静态计数仍不构成 Population 完成。
