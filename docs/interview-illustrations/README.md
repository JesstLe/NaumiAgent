# 教材教学配图

已使用内置 `image_gen.imagegen` 生成并逐张检查 27 张教学图，均已插入本地对应章节。具体模型标识未由工具返回，不标注为 image2.5。

飞书状态：图片尚未插入。Chrome 扩展的文件上传返回 `Not allowed`；需要开启该扩展的“允许访问文件网址”后继续原位上传，不创建重复章节，不扩大知识库权限。

## 文件与复核

- `prompts.json` 保存首版提示词；`manifest.json` 保存实际选中版本的完整提示词、图片路径、章节、读图说明与独立同步状态。
- 图片经中文标签、箭头和章节含义检查；有误的首版未放入教材，生成工具原始文件仍保留。
- 图片是教学示意，不代替项目源码、测试结果或原有详细讲解。图下注释明确实现范围与证据边界。
- 在仓库根目录运行 `node docs/interview-illustrations/validate.mjs`，检查覆盖数、PNG 完整性、尺寸、唯一性、章节链接与图注一致性；同时如实报告正文相对首次插图前基准是否变化。自动检查不代替视觉与知识校对。
- 2026-09-11 经用户要求，正文增加大厂求职定位与逐章项目深挖，因此当前 `original_text_preserved` 应为 `false`，不能继续宣称正文未改。历史 `verification.json` 只记录首次插图时的检查结果，未覆盖本轮编辑。若专门复核“仅插图、不改正文”的历史版本，可使用 `--require-original-text` 强制比对；在当前修订版运行该选项预期失败。原图与图注保持不变。

## 逐章入口

| 编号 | 教材章节 | 教学图 |
| --- | --- | --- |
| 00 | [从最小闭环到工程化 Agent](../../docs/interview/00-agent-architecture-2026.md) | [打开图片](../../docs/interview/assets/illustrations/00-teaching-v2.png) |
| 01 | [愿望、验收与授权是三件事](../../docs/interview/01-目标与交互层.md) | [打开图片](../../docs/interview/assets/illustrations/01-goal-v1.png) |
| 02 | [一次任务如何推进与停下来](../../docs/interview/02-Runtime与状态机.md) | [打开图片](../../docs/interview/assets/illustrations/02-teaching-v2.png) |
| 03 | [模型档位不等于智能路由](../../docs/interview/03-模型与推理路由.md) | [打开图片](../../docs/interview/assets/illustrations/03-teaching-v2.png) |
| 04 | [一个上下文窗口该装什么](../../docs/interview/04-上下文工程.md) | [打开图片](../../docs/interview/assets/illustrations/04-teaching-v1.png) |
| 05 | [四种记忆，四种职责](../../docs/interview/05-记忆与持久化.md) | [打开图片](../../docs/interview/assets/illustrations/05-teaching-v1.png) |
| 06 | [计划可变，执行事实要核对](../../docs/interview/06-Plan与任务账本.md) | [打开图片](../../docs/interview/assets/illustrations/06-teaching-v2.png) |
| 07 | [从工具调用到业务结果](../../docs/interview/07-工具与行动层.md) | [打开图片](../../docs/interview/assets/illustrations/07-teaching-v1.png) |
| 08 | [权限应在真正执行时生效](../../docs/interview/08-安全身份与授权.md) | [打开图片](../../docs/interview/assets/illustrations/08-teaching-v2.png) |
| 09 | [为什么超时后不能直接重试](../../docs/interview/09-可靠性与恢复.md) | [打开图片](../../docs/interview/assets/illustrations/09-teaching-v1.png) |
| 10 | [运行支撑与评测支撑分工](../../docs/interview/10-可观测性Harness与评测.md) | [打开图片](../../docs/interview/assets/illustrations/10-teaching-v2.png) |
| 11 | [MCP 连接能力，Skill 提供方法](../../docs/interview/11-MCP与Skills.md) | [打开图片](../../docs/interview/assets/illustrations/11-teaching-v1.png) |
| 12 | [浏览器操作是观察与核验循环](../../docs/interview/12-Browser与ComputerUse.md) | [打开图片](../../docs/interview/assets/illustrations/12-teaching-v2.png) |
| 13 | [子 Agent 与 Worker 不是同一种角色](../../docs/interview/13-多AgentWorker与集群.md) | [打开图片](../../docs/interview/assets/illustrations/13-teaching-v1.png) |
| 14 | [改进候选必须允许被拒绝](../../docs/interview/14-演进与发布治理.md) | [打开图片](../../docs/interview/assets/illustrations/14-teaching-v2.png) |
| 15 | [比较 Agent，先对齐层次](../../docs/interview/15-主流Agent对比与定位.md) | [打开图片](../../docs/interview/assets/illustrations/15-teaching-v2.png) |
| 16 | [一段项目介绍背后的证据链](../../docs/interview/16-NaumiAgent项目介绍与答题路线.md) | [打开图片](../../docs/interview/assets/illustrations/16-teaching-v1.png) |
| 17 | [用一个任务读懂 Agent 术语](../../docs/interview/17-阅读方法与术语.md) | [打开图片](../../docs/interview/assets/illustrations/17-teaching-v1.png) |
| 18 | [一项结论需要哪种证据](../../docs/interview/18-评审与验证记录.md) | [打开图片](../../docs/interview/assets/illustrations/18-teaching-v2.png) |
| 19 | [从编程基础到六项学习成果](../../docs/interview/19-学习定位与成果路线.md) | [打开图片](../../docs/interview/assets/illustrations/19-teaching-v1.png) |
| 20 | [周报草稿助手的学习主线](../../docs/interview/20-实战主线与环境手册.md) | [打开图片](../../docs/interview/assets/illustrations/20-teaching-v2.png) |
| 21 | [换业务，不换工程检查方法](../../docs/interview/21-业务场景迁移实验.md) | [打开图片](../../docs/interview/assets/illustrations/21-teaching-v1.png) |
| 22 | [怎样形成自己的代码贡献](../../docs/interview/22-独立改造任务与参考思路.md) | [打开图片](../../docs/interview/assets/illustrations/22-teaching-v2.png) |
| 23 | [公平实验与诚实结论](../../docs/interview/23-评测故障与实验报告.md) | [打开图片](../../docs/interview/assets/illustrations/23-teaching-v1.png) |
| 24 | [面试练习是可追问的展示](../../docs/interview/24-求职训练与能力自测.md) | [打开图片](../../docs/interview/assets/illustrations/24-teaching-v1.png) |
| 25 | [五个实验都通过，不等于五项业务都成功](../../docs/interview/labs/README.md) | [打开图片](../../docs/interview/assets/illustrations/25-teaching-v2.png) |
| nav | [一本教材怎样变成自己的项目能力](../../docs/interview/README.md) | [打开图片](../../docs/interview/assets/illustrations/nav-teaching-v1.png) |
