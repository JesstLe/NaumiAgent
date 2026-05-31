# NaumiAgent 容器化部署

目标：让新用户 clone 仓库后，只配置必要密钥，就能启动一个可用的 NaumiAgent REST API 服务。

## 前置要求

- Docker Engine 或 Docker Desktop
- Docker Compose v2
- 一个兼容 LiteLLM/OpenAI 格式的模型 API Key

## 1. 初始化配置

```bash
cp .env.example .env
mkdir -p workspace
```

编辑 `.env`，至少填入：

```bash
NAUMI_MODELS__API_KEY=你的模型 API Key
```

公开部署时不要保持 `NAUMI_API__API_KEYS=[]`。建议设置：

```bash
NAUMI_API__API_KEYS=["change-this-key"]
```

Linux 用户如果要让容器写入 `./workspace` 后仍保持当前用户所有权，建议再设置：

```bash
HOST_UID=$(id -u)
HOST_GID=$(id -g)
```

## 2. 启动

```bash
docker compose up --build
```

启动流程包含两个服务：

- `naumi-bootstrap`：一次性引导服务，校验配置、创建 `/app/data` 和 `/workspace`，缺少模型 Key 会直接失败。
- `naumi-api`：FastAPI 服务，默认监听 `http://127.0.0.1:8080`。

健康检查：

```bash
curl http://127.0.0.1:8080/api/v1/health
```

API 文档：

```text
http://127.0.0.1:8080/docs
```

## 3. 常用命令

```bash
make bootstrap   # 复制 .env.example、创建 workspace、运行引导校验
make up          # 构建并启动
make logs        # 查看 API 日志
make health      # 请求健康检查
make down        # 停止服务
```

## 4. 配置文件

容器默认使用：

```text
deploy/config.container.yaml
```

关键路径：

- 会话数据库：`/app/data/sessions.db`
- 向量数据库：`/app/data/chroma`
- Agent 工作区：`/workspace`，映射到宿主机 `./workspace`

密钥和环境相关配置通过 `.env` 注入，避免把 secret 写入 YAML。

## 5. API 鉴权

如果 `.env` 中配置了：

```bash
NAUMI_API__API_KEYS=["change-this-key"]
```

调用受保护接口时传入：

```bash
curl -H 'X-API-Key: change-this-key' http://127.0.0.1:8080/api/v1/tools
```

WebSocket 也支持：

```text
ws://127.0.0.1:8080/api/v1/ws/sessions/<session_id>?api_key=change-this-key
```

## 6. Browser 能力

镜像内已安装 Playwright Chromium，NaumiAgent 内置浏览器工具可以直接使用。

`browser-debugging-daemon` 外部适配在容器配置中默认关闭：

```yaml
browser_daemon:
  enabled: false
```

如果你单独部署了 browser-debugging-daemon，可以把 `base_url` 指向对应服务，并通过环境或自定义配置覆盖。

## 7. 故障排查

配置校验：

```bash
docker compose run --rm naumi-bootstrap
```

进入容器：

```bash
docker compose run --rm naumi-api bash
```

查看日志：

```bash
docker compose logs -f naumi-api
```

如果容器启动后立即退出，优先检查 `.env` 是否存在、`NAUMI_MODELS__API_KEY` 是否仍是占位值。
