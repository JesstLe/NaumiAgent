# Web2 恢复 Kimi Coding 配置

日期：2026-09-11

用户要求恢复最初的 `kimi-for-coding`，本机两个启动配置入口均已调整：

- 根目录 `config.yaml`：新增 Kimi 模型配置，继续使用根目录 `data`。
- `.naumi/config.yaml`：恢复 Kimi 配置并移除旧 OpenCode `catalog_path`，保留其他配置和 `.naumi/data`。
- 默认、快速、推理档统一为 `openai/kimi-for-coding`；接口为 `https://api.kimi.com/coding/v1`，temperature 为 1.0。
- 原本机配置已备份至 `.naumi/data/config-backups/`，未写入明文密钥。配置与验证证据均为 Git 忽略的本机文件。

## 验证与运行状态

通过实际配置加载和 `ModelRouter.resolve_model()` 核对两个入口的三档模型、接口和数据路径。
用户补充有效凭据后，已保存到系统凭据库的 Kimi 专用账户并完成读取校验。
两个本机配置显式设置 `models.api_key: null`，经实际加载验证使用系统凭据，避免当前进程中的旧通用环境密钥覆盖。
使用项目默认 `ModelRouter.call()` 调用成功，实际回复 `Kimi connected.`，供应商返回模型为 `kimi-for-coding`。
验证发现接口按思考模式约束 temperature：默认开启思考时要求 1.0，显式关闭思考时要求 0.6。最终保留项目原预设及默认思考模式，无需修改源码。验证证据位于 `.naumi/data/kimi-credential-check.json`，不包含密钥。

8765 后端已在用户授权范围内重启并加载当前配置。后续真实 Web2 多轮验收继续使用
`openai/kimi-for-coding`，产生并恢复了分阶段工具记录与公开摘要；模型隐藏 reasoning
未写入前端事件或会话持久化。修改本机模型配置后仍需重启后端才能生效。

与「Web2 UI」任务已同步本次配置结果；该任务继续负责按用户消息关联历史执行时间线。
