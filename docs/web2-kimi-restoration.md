# Web2 恢复 Kimi Coding 配置

日期：2026-09-11

用户要求恢复最初的 `kimi-for-coding`，本机两个启动配置入口均已调整：

- 根目录 `config.yaml`：新增 Kimi 模型配置，继续使用根目录 `data`。
- `.naumi/config.yaml`：恢复 Kimi 配置并移除旧 OpenCode `catalog_path`，保留其他配置和 `.naumi/data`。
- 默认、快速、推理档统一为 `openai/kimi-for-coding`；接口为 `https://api.kimi.com/coding/v1`，temperature 为 1.0。
- 原本机配置已备份至 `.naumi/data/config-backups/`，未写入明文密钥。配置与验证证据均为 Git 忽略的本机文件。

## 验证与尚未完成事项

通过实际配置加载和 `ModelRouter.resolve_model()` 核对两个入口的三档模型、接口和数据路径。
使用项目 `ModelRouter.call()` 发送一次无工具的简短连接请求，Kimi 返回 HTTP 401 `AuthenticationError`。
当前进程存在 `NAUMI_MODELS__API_KEY`，但本项目没有 `.env`，系统凭据库也没有 Kimi 专用凭据。
因此尚未验证 Kimi 成功回复，需要更新有效的 Kimi Coding 凭据后再验证。

既有 8765 后端仍加载旧配置。此前停止并重启该进程的动作被自动审批以 `blocked by policy` 拒绝，本次未重试或绕过。
需要重新启动后端才能加载本次配置，并再次检查 `/api/v1/config` 和 Web2 实际对话。

与「Web2 UI」任务已同步本次配置结果；该任务继续负责按用户消息关联历史执行时间线。
