# PA-03：Google 接入准备与撤销反馈

日期：2026-11-05  
基线：`90b59cdc8d837503a200175582c8be4f9ea0b080`  
事务：`.naumi/transactions/pa03-google-revoke-20261105/`  
状态：首个平台已由用户选定为 Google Calendar；撤销反馈修订完成。真实账户闭环仍待独立验收。

## 实际本机准备状态

本轮按项目配置、正常 Settings 优先级和运行态路径解析实际个人目录，而不是只猜默认目录。
只读预检得到 `NOT_INITIALIZED / client_configured=false / account_count=0`。没有读取客户端明文、
token 或系统凭据，没有启动 worker、网络或 provider，个人目录完整文件指纹前后相同。
配置在进程内解析；记录只保存公开布尔、数量和目录不可逆标识，不输出配置值或凭据。
metadata 状态不证明系统凭据可读，`credential_readability=NOT_CHECKED` 保持独立。

本轮私密配置卡终态为 `timed_out`，没有收到客户端文件，没有初始化或连接用户的真实账户。

新的桌面 installed OAuth 客户端配置通过私密卡收集；不在聊天里索取账号密码。
导入、Google 登录/只读授权、显式写权限升级和确切动作批准是不同步骤，不能自动合并。
若配置未到位，真实 Google 读取、事项/草稿、人工批准、外部写入、原对象读回和远端撤销仍保留 NOT_RUN。

## 已定位的真实接口接缝

`ConnectionAuthority.disconnect()` 先本地断连、fence 旧授权，再按原 generation 单次请求 revoke。
服务确认返回时保存 `confirmed`，已知失败保存 `error`，不确定保存 `unknown`，没有请求保存 `not_requested`。
原 UI 只认识 legacy `revoked/failed`，因此对确切已确认和已知失败都误显“待核对”。

修订仅补齐 `confirmed → 已撤销`、`error → 撤销失败`；保留未知与无状态、legacy 状态和未来状态的行为。
没有改 token 生命周期、协议返回值、模型权限、审批、CLI 或外部重试。显示更新不触发任何账户动作。

## 组件与构建证据

- 原源码加新增状态用例：`2 failed / 10 passed / 49 skipped`，exit 1；两个失败精确对应 confirmed/error。49 项是 `-t` 名称过滤未运行，不计作通过。
- 修后完整连接组件：`61 passed`，exit 0；包括12项 canonical/legacy/未知/无状态与零副作用断言。
- 全前端：`33 files / 411 tests`，exit 0。与组件组存在重叠，不相加作为独立覆盖数。
- TypeScript：exit 0。实际生产构建、协议快照/浏览器、源码副本回滚与终态命令以本事务 `VERIFICATION.txt` 为准。
- 中间一次摘要打印的 GBK 异常使 patch 未执行，但链继续运行误标 modified 的测试；保留其2项失败，不算修后证据。

## 独立真实协议与浏览器证据

从旧协议夹具复用 RSA/nonce/Google 协议实现，所有运行目录、端口、凭据和资料库显式重绑定到本事务新目录；
旧夹具源码与旧 browser-data 不改动。新夹具保留零非 loopback、零模型、零原生用户 keyring 的 tripwire，
仅启动空 Agent 目录，没有预置 Agent、actions 或 jobs。

22 次真实本地 API 请求完成：新 fixture 客户端配置→只读 OAuth→实际 loopback 签名 callback→原 connection→
`remote_revoke:true`→实际本地 `/revoke` HTTP200→按原 ID 读回 `confirmed`。同请求重放不新增 revoke，
after-domain 与 replay-domain 的文件 SHA 相同。这是 `LOCAL_PROTOCOL_FIXTURE`，不是 Google 真实账户。

独立浏览器在同一原 connection 上先载入旧生产 bundle，实际显示“远端撤销：待核对”；构建新生产 bundle 后
仅重新读取同一个对象，显示“远端撤销：已撤销”。前后公开 connection DTO 完全一致，浏览器未执行账户写入，
实际远端协议 revoke 总数仍为 1。断开账户的 events 读取返回409并展示已断开提示，此预期诊断没有触发重试。
`google-revoke-browser.png` 已保存并打开检查；标签已关闭，自身服务器精确 PID 的进程退出码核为0。

此轮没有使用或读取用户的 Google 凭据，没有新增付费 Run；真实账户及发布部署仍保持各自待验收状态。

## 真实闭环的验收合同

1. 实际 Google 桌面客户端 JSON 进入系统凭据库加密生命周期，不进入浏览器持久存储。
2. 用户完成只读 OAuth，读取原账户主日历事件及确切来源版本；相同事件去重转真实事项/草稿。
3. 用户单独确认写权限升级，再审阅并批准确切草稿 revision/digest、账户和有效期。
4. 既有 Action Job/Run 只派发原对象一次；写后按原 event ID 读回字段和 marker。
5. 响应未知先 GET 原对象对账，不重发；断连与远端撤销分别记录，撤销后停止使用旧来源。

本地协议夹具、原生凭据库、浏览器、真实账户和发布部署分别验收；本页不把其中一层替代另一层。
初始仅主 Agent，其他持久 Agent 仍由用户要求创建，本模块不创建或重命名身份。
