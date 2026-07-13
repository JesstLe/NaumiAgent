# Anthropic Messages 适配器实施计划

1. 为 transport dispatcher、Anthropic 标准认证与协议错配写失败测试。
2. 实现 `build_anthropic_messages_transport()`，复用现有受限凭据读取和 header 冲突检查。
3. 为 Router 非流式、工具调用和流式请求写失败测试并接入 transport。
4. 使用本地 loopback 服务真实运行 LiteLLM 的 `/v1/messages` 文本、工具与 SSE 链路。
5. 只运行 provider runtime、model router transport 等相关小模块测试，完成 Ruff、编译、自审、提交和推送。
