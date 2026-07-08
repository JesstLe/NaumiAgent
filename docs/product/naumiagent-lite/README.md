# NaumiAgent Lite 交付包

这是一套围绕 NaumiAgent 项目整理出来的「工程级 Agent 项目」交付材料，目标是让买家不只是拿到一份源码，而是能把项目跑起来、讲清楚、写进简历，并能在面试里回答关键追问。

## 这个项目卖的是什么

卖点不是「又一个 AI 聊天机器人」，而是一个本地 Coding Agent 系统的工程拆解：

- 有 Agent Runtime：能讲 ReAct 主循环、任务编排、工具调用历史。
- 有 Tool Registry：不是只写 prompt，而是把文件、Shell、浏览器、记忆、任务等能力做成工具。
- 有安全边界：权限、预算、guardrails、危险操作确认。
- 有长期状态：SQLite 会话、长期记忆、上下文压缩、运行态面板。
- 有扩展能力：MCP、browser daemon、subagent、scheduler、self-review、self-modify、forge、pursuit。

一句话定位：

> 从 toy demo 到工程级本地 Coding Agent，一套能运行、能讲架构、能写简历、能二开的项目包。

## 适合谁

- 后端同学：想把项目从「管理系统/秒杀/RPC」升级到 AI 工程项目。
- AI 应用开发求职者：不想只写 RAG、客服、知识库。
- 低年级学生：需要一个能长期迭代的主线项目。
- 已经做过 LangChain demo 的人：想补工具系统、权限系统、记忆系统、MCP 这些工程深度。

不适合：

- 只想复制粘贴交差的人。
- 完全不愿意本地运行项目的人。
- 期待买完资料就直接获得面试结果的人。

## 你会拿到什么

本交付包包含九份材料：

- `商品页文案.md`：用于上架的商品详情页和价格策略。
- `小红书笔记矩阵.md`：30 条内容选题，覆盖种草、转化、面试、架构。
- `课程大纲.md`：7 天学习路线。
- `简历包装.md`：后端、AI 应用、Agent 工程三套简历写法。
- `面试题库.md`：60 个高频追问和参考回答。
- `项目讲解稿.md`：30 秒、3 分钟、8 分钟讲解稿。
- `交付检查清单.md`：卖家和买家的交付验收表。
- `二开任务路线.md`：从简单工具到 Workbench 治理能力的二开路线。
- `README.md`：你正在看的使用入口。

## 如何使用这套材料

建议按这个顺序走：

1. 先看 `课程大纲.md`，把项目能力和学习路线串起来。
2. 按项目 README 跑通 NaumiAgent：`uv sync --extra dev`，再执行 `naumi chat` 或 `python -m naumi_agent.main chat`。
3. 看 `项目讲解稿.md`，先背 30 秒版和 3 分钟版。
4. 看 `简历包装.md`，选择一个版本改成自己的经历。
5. 刷 `面试题库.md`，把回答改成自己的表达。
6. 想继续变强，再按 `二开任务路线.md` 做 1-2 个功能。

## 推荐演示命令

这些命令适合录屏或面试前演示：

```bash
uv sync --extra dev
python -m naumi_agent.main chat
```

进入交互后可以演示：

```text
/help
/runtime
/memory stats
/doctor
/self-review src/naumi_agent/tools
/pursue "检查当前项目里最适合二开的 Agent 能力"
```

如果要展示 API：

```bash
naumi serve
```

如果要展示测试意识：

```bash
uv run pytest tests/unit/test_engine.py -q
uv run pytest tests/unit/test_tool_registry.py -q
uv run pytest tests/unit/test_permissions.py -q
```

## 你最终要能讲清楚什么

买家学完后，至少应该能讲清楚这五件事：

1. 为什么普通 Agent demo 容易显得 toy。
2. NaumiAgent 的 Runtime、Tool、Memory、Safety 分别解决什么问题。
3. 工具调用为什么要有权限、预算、结果结构化。
4. MCP、浏览器自动化、自我修改为什么是高级扩展点。
5. 如果继续二开，应该先补评测闭环，而不是盲目堆功能。

## 诚实边界

这套项目适合做「工程能力展示」和「面试表达训练」，不承诺任何录用结果。简历里的指标、截图、运行结果需要买家自己本地验证后再写。对于源码里属于雏形或二开方向的部分，材料会明确标出，不建议包装成已经大规模生产可用。

