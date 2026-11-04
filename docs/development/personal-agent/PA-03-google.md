# PA-03 Google 协议组件

日期：2026-11-05  
基线：`281be9382e7fdb2e310c15e7d9e0f86e32e6c946`  
状态：协议组件已实现；本地 HTTP/socket/签名验证最终 **86 passed**，指定四文件 ruff 与组件 import 通过。**真实 Google 账户验收：NOT_RUN。**

本组件只实现 Google 固定端点的 OAuth、短期 loopback transport、Calendar HTTP adapter、事件身份与读回投影。ConnectionStore、token 续期 CAS、账号/Agent grant、同步游标事务、事项 outbox、草稿/审批/action ledger、API/Tool/CLI/TUI 和实际账户验收由 PA-03 的共享 service 层负责；本页不将协议通过写成整个模块完成。

## 1. 文件与接口

- `src/naumi_agent/personal/google_oauth.py`
- `src/naumi_agent/personal/google_calendar.py`
- `tests/unit/test_personal_google_oauth.py`
- `tests/unit/test_personal_google_calendar.py`

依赖声明与锁文件由 Root 管理：`google-auth[requests]>=2.40,<3`；本轮运行环境由 Root 按锁安装 `google-auth==2.62.1`。本组件不安装依赖、不读取配置或现有凭据、不访问个人服务账户。

### OAuth

```python
oauth = GoogleOAuthClient.from_json(
    desktop_client_json,  # str or dict; installed, not web
    http_client=None,     # optional owned-by-caller httpx.AsyncClient
    cert_request=None,    # optional certificate transport for protocol tests
)
url = oauth.authorization_url(
    redirect_uri, state, nonce, code_challenge, write=False,
)
tokens = await oauth.exchange_code(code, redirect_uri=redirect_uri, code_verifier=verifier)
tokens = await oauth.refresh(refresh_token)
claims = await oauth.verify_id_token(tokens["id_token"], nonce=nonce)
await oauth.revoke(token)
```

`client_id` 可公开；`client_secret` 不进入 repr。配置限 16 KiB、只允许 installed 结构及已知 Google 字段，固定 Google auth/token/certificate 地址；不接受用户任意 provider URL。接受导出的 Google 旧 auth URI，但实际授权 URL 固定使用 v2 endpoint。

只读 scope：`openid email https://www.googleapis.com/auth/calendar.events.owned.readonly`。写 scope：`openid email https://www.googleapis.com/auth/calendar.events.owned`。使用 PKCE S256、`access_type=offline`、`prompt=consent`，**不发送 `include_granted_scopes`**。Google 原生 installed 应用不支持增量 OAuth 授权，service 必须在写权限升级时显式重新授权同一个已验证 issuer/sub；事件增量同步不受此结论影响。

code exchange、refresh、revoke 都没有自动重试。token response 流式上限 256 KiB，所有请求明确禁止 redirect，即使注入 client 的默认设置允许 redirect 也不继承。refresh 响应不含 refresh token 时，adapter 不虚构、不复用旧值；同身份 token generation 的继承与 CAS 由上层负责。

### ID token

通过维护中的 `google.oauth2.id_token.verify_oauth2_token` 验证真实签名、Google issuer、client audience、iat/exp；组件再核对 nonce、azp、稳定 sub、email 与严格布尔 `email_verified=True`。验证在 `asyncio.to_thread` 中完成，避免同步证书获取阻塞事件循环。不存在未验签 decode 后相信 claims 的路径。

默认证书请求只访问固定 Google X509 endpoint，关闭环境代理继承、重定向和自动重试，证书响应限制 256 KiB。注入证书 transport 仍经过 endpoint、状态、大小和 X509 map 结构检查。明确拒绝 JWK `keys` 文档，避免维护库在该分支另走 `PyJWKClient` 发起绕过受控 transport 的第二次获取。

### 安全错误合同

```python
except GoogleProtocolError as exc:
    code = exc.code
    status = exc.status       # int or None
    unknown = exc.unknown
    retryable = exc.retryable
```

错误字符串只有受限格式的稳定 code；不包含 token、client secret、响应正文、provider message 或请求 URL。HTTP 401/403 scope/ACL/quota、409、410、412、429、5xx 有离散分类。提交后网络异常、写操作 5xx、redirect、超限/无效成功响应标 `unknown=True`；组件不会把结果不明改成“未受理”。GET 404 只是协议观测，**不是原写操作未创建的证据**。

## 2. Loopback

```python
listener = OAuthLoopback(
    state=state,
    expires_at=aware_utc_expiry,
    path=None,                  # default random /oauth/<opaque>
    max_request_bytes=8192,
)
await listener.start()
redirect_uri = listener.redirect_uri
code = await listener.wait(timeout=None)
await listener.close()
```

也支持 `async with OAuthLoopback(...) as listener`。

- 只绑定 `127.0.0.1`、随机端口，精确 path/Host/state/时限；不开放 FastAPI 匿名前缀。
- code callback 和 `wait()` 交付各自只消费一次；并发 waiter 只有一个拿到 code。
- 错 state、错误 path/Host、重复 query、Unicode state、OAuth error 和超长 header 不消费合法意图，用户可在原时限内继续。
- 请求头有字节上限和读取时限；expiry/close 会关闭 listener 与仍在读取的连接。
- 响应固定中文纯文本、no-store、CSP、nosniff，不反射 callback 输入；没有可执行 HTML 或 URL/body 注入。
- 取消等待不等于 code exchange 未发生；loopback 仅传 code，不负责批准外部动作或选择账户。

## 3. Calendar HTTP adapter

```python
calendar = GoogleCalendarAdapter(http_client=None)
page = await calendar.list_events(access_token, page_token=None, sync_token=None)
page = await calendar.instances(
    access_token, event_id, page_token=None, time_min=None, time_max=None,
)
event = await calendar.get_event(access_token, event_id)
event = await calendar.insert_event(access_token, client_event_id, body)
event = await calendar.update_event(access_token, event_id, body, etag=original_etag)
```

固定 HTTPS endpoint 和 `primary`，不接受任意账户资源 URL。`list_events` 固定 `singleEvents=false/showDeleted=true/maxResults=250`，仅显式 page/sync token；不与 sync token 混用禁止的时间或搜索过滤条件。`instances` 单独承载有界时间窗口。每次调用只读取一页，不自作主张循环或推进上层游标。Calendar response 流式上限 2 MiB。

创建 ID 为合法 base32hex，先确定再发送；创建/修改均显式 `sendUpdates=none`。修改必须提供 `If-Match`。HTTP 层不重试，不在 409/412/timeout 后改 ID、自动合并或再次写入。

写 body 仅接受 summary/description/location/start/end/transparency/visibility/extendedProperties，必须携带 `extendedProperties.private.naumi_operation_id`。创建必须有同类型且严格递增的 start/end。写入前经过校验并 JSON 脱钩复制，调用者在 await 期间修改嵌套 dict 不改变请求对象。与会者、附件、会议、周期编辑等不进入首个无与会者草稿写合同。

## 4. 事件标准化与读回

`normalize_event(raw)` 返回：

```text
id, status, etag, updated,
summary, description, location, transparency, visibility, htmlLink,
start, end, recurringEventId, originalStartTime,
iCalUID, sequence, recurrence,
extendedProperties: {private: {...}}
```

- 时间为 `{date: YYYY-MM-DD, timeZone?: IANA}` 或 `{dateTime: UTC ISO8601 with microseconds Z, timeZone: IANA-or-None}`，保留全天与时间点的类型差异；全天可选时区保持原样、不转换到 UTC 前一天；cancelled tombstone 可缺 start/end，返回 None。
- `normalize_event` 幂等；已标准化对象可再传给标准化或 occurrence key。
- `occurrence_key`：非周期为 `event:<eventID>`；周期为 `instance:<recurringEventId>:<canonical originalStartTime>`。移动实例、改标题不产生新身份；不同重复实例不因共享 iCalUID 合并。
- `approved_event_projection` 只处理批准字段，验证日期、时区、字段与正起止区间。
- `event_matches_approval(event, approved, operation_marker=...)` 同时核对双方 private marker 与批准字段。无与会者单次事件的起止按精确 UTC instant 比较，允许 Google 对 offset、UTC/IANA 别名和默认值的表示规范化；原始 timeZone 仍保存在快照中，不将该比较规则用于周期修改。
- provider 自动补充 created、organizer、reminders 等字段不导致错误冲突；provider 出现未批准的 attendees、附件、会议或 recurrence 则拒绝匹配。summary/description/地点、起止类型或实际 instant、marker 变化都会失败。

## 5. 测试及证据等级

测试使用实际 `ThreadingHTTPServer`、TCP socket、HTTPX transport 转接。组件自己仍构造固定 Google URL；测试 transport 将实际请求转到专用本机 fixture，不开放产品 endpoint 配置。证书测试生成真实 RSA/X509 和签名 JWT，由维护库经一次真实本机证书 HTTP 获取完成验签。

覆盖：Desktop 配置/大小/URL 边界、PKCE/scopes、code/refresh/revoke、无 refresh token 不虚构、丢包/redirect/503/超限/重复 JSON key、签名/issuer/audience/exp/nonce/email_verified、JWK 二次获取阻断、loopback 错误/重复/并发/时限/Host/超长/半连接关闭；Calendar 分页/同步参数/instances/原对象/If-Match、全部错误分类、未知写单次派发、非法写零派发、周期/DST offset/全天与 tombstone、规范化幂等、批准投影与 operation marker。

首次组件组：`67 passed in 19.48s`。随后自审发现并补强固定证书 transport 的 JWK 分支、防绕取证书、错误码约束、provider 缺省字段、归一化幂等、非法写前置拒绝及 loopback 关闭边界；不是只验证 import 后交差。首轮 ruff 暴露排版和命名问题，已修复；当前指定四文件 `All checks passed!`。

最终验证命令：

```powershell
.\.venv\Scripts\ruff.exe check src/naumi_agent/personal/google_oauth.py src/naumi_agent/personal/google_calendar.py tests/unit/test_personal_google_oauth.py tests/unit/test_personal_google_calendar.py
.\.venv\Scripts\python.exe -m pytest tests/unit/test_personal_google_oauth.py tests/unit/test_personal_google_calendar.py -q --tb=short
```

最终扩充结果（exit 0）：

```text
All checks passed!
86 passed in 27.29s
```

组件导入检查（exit 0）：

```powershell
.\.venv\Scripts\python.exe -c "import naumi_agent.personal.google_oauth, naumi_agent.personal.google_calendar; print('GOOGLE_COMPONENT_IMPORT_OK')"
```

```text
GOOGLE_COMPONENT_IMPORT_OK
```

保留中间失败：扩充组曾为 `1 failed, 84 passed in 27.69s`，失败源于半连接关闭测试过度要求“必须零响应字节”；实际 listener 已关闭连接，并可能发送固定 400/410 拒绝回执。测试改为验证有界结束、空响应或固定拒绝响应，仍不允许成功消费；随后 `85 passed in 27.30s`。再补全天可选时区原样保留回归后，得到上述最终 86 项。没有把这一中间失败记成 Google 账户问题，也没有弱化 state/消费守卫。

**账户与外部效果：NOT_RUN。** 没有取得真实 Google consent、读取个人日历、交换实际账户 code、续期/撤销实际 token 或创建实际 event。这些是 Root 的 L3 账户验收门；本机真 HTTP/socket fixture 不升格为 Google 账户已连接，也不证明长期 refresh/consent 发布状态。
