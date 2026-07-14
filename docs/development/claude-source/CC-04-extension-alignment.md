# CC-04 Plugin、Skill、MCP 扩展机制对齐

## 目标

比较并对齐 Claude Code 的 plugin/skill/MCP 发现、信任、启用、命令与工具暴露机制，同时保留
NaumiAgent 自身 Tool metadata、PermissionChecker 和配置布局。

## 子模块

- CC-04.1 Discovery model：系统/用户/workspace 来源、优先级、冲突命名。
- CC-04.2 Manifest contract：id/version/capabilities/entrypoints/config/secrets/platform。
- CC-04.3 Trust/install：预览文件、依赖、命令、网络、权限；用户明确确认。
- CC-04.4 Runtime isolation：失败隔离、超时、卸载、热重载边界。
- CC-04.5 UI surfaces：列表、详情、启停、错误、来源、升级提示。
- CC-04.6 Compatibility adapter：能复用的 skill/MCP 语义和明确不兼容项。

## 验收标准

- 同名扩展按来源规则确定且向用户解释，不能静默覆盖。
- workspace 扩展默认不信任；bypass 不自动安装未知代码。
- secret 只保存引用，不进入 manifest、日志、Harness 或自进化数据。
- 扩展崩溃不终止 Runtime；禁用后不再暴露 Tool/command。
- 真实安装、启用、调用、失败、禁用、卸载和重启恢复流程通过。

## 非目标

不承诺 Claude Code 私有 API 二进制兼容；优先对齐公开可验证行为。
