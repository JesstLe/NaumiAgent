# EVO-01.1b Self-Review 静态证据适配器

## 目标

把现有 `self_review` 从主要输出人类可读字符串，升级为“结构化 AST 事实 → 脱敏展示 → Evolution Evidence”的同源链路。模型增强仍可提供 hypothesis，但不能成为 hard evidence。

## 结构化扫描

`scan_self_review_files()` 对声明工作区内的 Python 文件逐个执行有界扫描：

- 文件必须真实存在且解析后的路径仍位于工作区，symlink escape 被拒绝；
- 单文件最大 2 MB，UTF-8 解码失败、超限和越界形成显式 scan error；
- 同一真实文件经不同输入路径出现时只扫描一次；
- 每个文件计算完整 SHA-256，并以带时区的文件修改时间作为 observation time；
- AST 检测 bare except、Exception/BaseException、硬编码 secret、公开函数缺返回类型、超过 60 行函数、模块级 list/dict/set 和 syntax error；
- finding 只保留 code、相对路径、行号、symbol、文件摘要和时间，不保留源码片段或 secret 值。

旧的 `scan_self_review()` 兼容入口继续返回字符串，但不再回显命中的密钥片段。真实 `SelfReviewTool.execute()` 改用结构化扫描渲染结果；发送给可选外部模型的源码会先遮蔽 secret assignment literal。

## Evolution Evidence

`adapt_self_review_static_evidence()` 将每个 finding 转成：

- `source_kind=self_review_static`；
- 与 finding code 一致的 `finding_code`，不伪造 Harness failure class；
- `artifact://workspace/<relative-path>` 引用与完整文件 SHA-256；
- `<relative-path>:<symbol>` scope；
- 由 code/path/symbol 生成的稳定 root fingerprint；
- 由 line、时间、文件摘要和 root fingerprint 生成的 observation id。

因此同一符号因文件行号移动仍保持同根，但文件内容或观察时间变化会形成新 observation。证据中不保存 LLM 输出，LLM 只能在 EVO-01.2 以后形成可审查 hypothesis。

## 验收标准

- 模块级与函数内部的 secret assignment 均能发现，但所有渲染、Evidence JSON 和模型输入都不含 secret value；
- bare except、宽泛异常、可变全局和未标注公开函数由 AST 定位；
- 相同未变文件重复扫描得到相同 Evidence；
- 只移动代码行会改变 observation id，不改变 root fingerprint；
- 工作区外文件和 symlink escape 不产生 finding，并留下脱敏错误码；
- Harness Evidence 升级后的通用契约仍通过既有测试；
- 仅运行 self_review/Evolution 小模块测试，不运行全量测试。

## 明确未完成

- 当前没有复杂度、依赖循环、重复代码、类型检查器或安全数据流分析；
- scan error 本身尚未适配为 Evolution Evidence，避免把环境问题伪装成代码缺陷；
- runtime metric、Eval 和用户 feedback adapter 仍未实现；
- Candidate schema、跨来源去重、资格判断、排序和 Review surface 仍属于 EVO-01.2—1.6。
