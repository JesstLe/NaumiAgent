# NaumiAgent 公网部署

公网部署使用三个容器：Bootstrap 校验运行目录和凭据，API 只在 Docker 内网监听，Nginx 发布 Web2 并同源反向代理 REST、SSE 与 WebSocket。公网只开放一个端口，浏览器不会直接接触模型 API Key。

## 启动

```powershell
Copy-Item .env.public.example .env.public
notepad .env.public
docker compose --env-file .env.public -f docker-compose.public.yaml up -d --build
docker compose --env-file .env.public -f docker-compose.public.yaml ps
```

必须设置：

- `NAUMI_MODELS__API_KEY`：模型提供商凭据，只进入 API 容器；
- `NAUMI_API__API_KEYS`：非空 JSON 数组，例如 `["随机长令牌"]`；
- `NAUMI_WORKSPACE_PATH`：服务器上允许 Agent 工作的目录。

默认入口为 `http://服务器公网IP:8080/web2`。首次打开后进入 **设置 → 连接**：

1. API 地址保留同源值 `https://你的域名/api/v1` 或 `http://服务器公网IP:8080/api/v1`；
2. “连接令牌”填写 `NAUMI_API__API_KEYS` 数组中的一个值；
3. 点击“保存并连接”。令牌仅保存在当前浏览器本地存储，并作为 Bearer Token 发送；WebSocket 使用同一令牌握手。

如果服务器位于家庭或办公网络，还需要在路由器上把公网 TCP 端口转发到运行 Docker 的主机；云服务器则需要在安全组和主机防火墙中只放行实际入口端口。公网 IP、域名解析和 NAT 端口转发由部署环境提供，NaumiAgent 不会自动修改路由器或云防火墙。

## HTTPS

生产公网应在 `NAUMI_PUBLIC_PORT` 前配置云负载均衡、Caddy、Traefik 或其他 TLS 终止层，并把流量转发到该端口。反向代理必须保留 WebSocket Upgrade 头，且外部只开放 HTTPS 入口。Web2 会自动把实时连接切换到当前页面的 `wss://` 同源地址。

## 运维命令

```powershell
# 查看状态与日志
docker compose --env-file .env.public -f docker-compose.public.yaml ps
docker compose --env-file .env.public -f docker-compose.public.yaml logs -f naumi-api naumi-web

# 更新代码后重建
docker compose --env-file .env.public -f docker-compose.public.yaml up -d --build

# 停止，但保留会话数据卷
docker compose --env-file .env.public -f docker-compose.public.yaml down
```

## 暴露边界

- `naumi-api:8080` 只有 Docker 内网可见；
- 公网入口只发布 Nginx 的 `NAUMI_PUBLIC_PORT`；
- Bootstrap 会同时拒绝缺少模型 Key和缺少客户端鉴权的配置；
- CORS 默认关闭跨域访问，分离部署前需设置精确的 HTTPS Origin；
- 通用会话 WebSocket 与 Workbench WebSocket 都会在 `accept` 前验证令牌。

## 已验证链路

本仓库的公网 Compose 方案已通过容器端到端验证：

- Bootstrap 缺少模型 Key 或客户端 API 令牌时拒绝启动；
- Web2 经 Nginx 返回 HTTP 200，静态资源携带 CSP、`X-Frame-Options: DENY` 和长期缓存头；
- `/api/v1/health` 经同源代理返回 HTTP 200；
- 受保护 API 未带令牌返回 HTTP 401，Bearer 令牌有效时返回 HTTP 200；
- 通用 WebSocket 未带令牌拒绝握手，带 `api_key` 时建立连接；
- Nginx 访问日志不记录查询字符串，公网 API 容器关闭 Uvicorn 访问日志，避免 WebSocket 查询令牌落盘；
- API 容器只有 Docker 内网 `8080/tcp`，宿主机只映射 Web 容器入口。
