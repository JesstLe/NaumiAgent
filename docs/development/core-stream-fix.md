# 核心流式输出一致性修复（F03 / F05）

## 完成内容

本模块修复当前底层审计的 F03（可见流/历史脱敏过晚）和 F05（断流后旧缓冲污染恢复结果）。

### 增量脱敏

- `IncrementalRedactor` 用小型状态机识别现有秘密样式，普通文本即时输出，仅保留可能成为秘密的短后缀。
- 候选达到长度阈值后立即输出占位符，持续消费该值的剩余字符，不将任意长秘密存入内部候选缓冲。
- 普通关键词前缀、跨 chunk 标签/引号/值、任意长分隔空白、未达阈值的普通文本及嵌套候选均有回归。
- GitHub token 消费完整字符段，不再仅遮盖前 36 个字符。引号标点被保留。
- 全文 `OutputGuardrail.redact()` 复用同一状态机，保证完整字符串与不同流式切块得到一致结果。
- 普通首块延迟不退化为固定尾窗或整段缓存。候选存储最大为 4 字符 GitHub 前缀加 35 字符未决值。

### 输出与上下文边界

- TOKEN 的直接发布、工具保护窗口 flush、最终补发及输出截断续写均走统一的响应 redactor；续写前不结束其状态，避免秘密横跨续写边界时泄露。
- 可见 THINKING_DELTA/THINKING_END 使用独立 redactor。
- `_append_message` 分离 provider 私有模型上下文与显示/持久化历史字典。assistant `content` 与 `reasoning_content` 的历史副本脱敏，模型私有 reasoning 保留用于同轮工具续接。
- 非流式最终结果同样保存脱敏文本。工具参数保持原值，执行语义没有改变。

### 断流恢复边界

- 尚未发布可见 TOKEN 时：允许自动回退，先清空失败流的文本、思考、未提交 redactor 状态和旧 tool-call 快照；恢复答案重新通过正常发布与持久化链路。
- 已发布可见 TOKEN 时：不再重跑并拼接另一份答案；明确返回 `failed`，保存已经发布的安全 partial，历史 metadata 标记 `interrupted=true`，发布中文中断信息并结束该响应。
- 该标记只放入显示/持久化历史，不注入模型消息字段。
- 使用实际发布片段作为提交边界，而不是仅依据 response_start 标志。
- 中断时不 flush 尚未判定的秘密后缀，不执行失败流残留的工具调用。
- 保持现有 append-only 传输协议，不要求客户端支持擦除或覆盖已收到的文字。

## 验证

未调用外部模型、未使用用户真实数据。模型传输用确定性响应/错误控制；实际引擎、事件管线、会话数据库与运行记录均执行真实代码，使用临时目录和 SQLite。

基线 6 项预期行为回归全部失败：

```text
6 failed, 23 deselected in 12.33s
PYTEST_EXIT=1
```

修复后集中运行：

```powershell
python -m pytest -q -p no:cacheprovider --tb=short `
  tests/unit/test_stream_integrity.py tests/unit/test_safety.py `
  tests/unit/test_engine.py::TestErrorRecovery `
  tests/unit/test_engine.py::TestEmptyModelResponseRecovery `
  tests/unit/test_engine.py::TestStreamingStartupLatency `
  tests/unit/test_engine_event_pipeline.py
```

```text
70 passed, 1 skipped in 22.21s
SUITE_EXIT=0
```

唯一 skip 为 `test_run_streaming_persists_only_current_terminal_release_context`：它使用 `test_release_launcher.py:32` 的 POSIX 真实执行夹具，在 Windows 上显式跳过；不是新增流式回归跳过。

新增覆盖包括：

- 所有已识别秘密样式的逐字符/每个二段切点一致性。
- 超长秘密、超长分隔符的有界候选状态与正常短首块即时性。
- TOKEN、THINKING、返回结果、持久化 assistant 历史的脱敏一致性。
- 工具参数不变，模型私有 reasoning 与历史副本不共享字典。
- 输出截断续写中的跨边界秘密。
- 发布前自动恢复、发布后失败、旧工具快照清理、回退再次失败。
- 公有 `run_streaming` 入口的真实 SQLite 运行记录为 failed，partial 历史具有中断标记，且不进行第二次模型调用。

`ruff check src/ tests/unit/test_stream_integrity.py` 与 import 检查通过。

## 自我审视与边界

- 没有通过缓存整段答案规避流式问题，也没有只修 TOKEN 而留下思考流绕过。
- 保存了正确的失败状态，没有把连接中断伪装成完成。
- 状态机仍是项目已定义样式的输出脱敏器，不是识别所有可能凭据形式的 DLP 系统。
- 本轮不重写既有数据库中的历史消息，不修改工具参数，不改变外部模型调用策略之外的任务权限。
- 这是组件及实际引擎/SQLite 链路验证，不是外部服务、所有客户端或全仓测试验收。
