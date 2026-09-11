# 共享能力层

对外包名为 `@naumi/shared`。

- `src/api`：API 契约、请求、SSE 传输。
- `src/hooks`：共享工作区控制器与 Provider，及原界面的兼容适配器。
- `src/platform`：浏览器／Tauri 平台接口。
- `src/stores`：共享会话状态投影。
- `tests`：上述公共能力的单元与契约回归测试。

本目录不依赖 `web` 或 `web2`。修改公共业务能力时在这里实现，再由不同界面绑定相同 action。
