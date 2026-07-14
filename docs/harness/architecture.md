# Harness 当前架构（H1-H2）

## 目标

Harness 负责把“仓库事实、用户意图、执行环境和验证边界”组织成 Agent 可可靠消费的工程上下文。H1/H2 的范围是安全加载 Profile、建立用户信任、确定性发现仓库知识，以及临时注入相关知识；它目前不是命令执行器，也不是完成判定器。

## 模块所有权

| 模块 | 单一职责 |
|---|---|
| `models.py` | 严格、冻结的 Profile 数据契约 |
| `profile.py` | 有界 YAML 读取、路径约束、精确 byte digest |
| `trust.py` | 工作区 + Profile digest 的用户级 SQLite 信任记录 |
| `knowledge.py` | 安全发现、Git 状态、文件 fingerprint、确定性排序、L2 读取 |
| `context.py` | L0/L1 渐进披露、模型窗口预算、闭合 Markdown 证据块 |
| `service.py` | 信任门、并发缓存、统一用户/Agent facade |
| `tools.py` | 三个只读 Agent Tools；不包含 trust/untrust |

Harness 包不得反向 import `AgentEngine`。Engine 只持有一个 `HarnessService`，并在每轮模型调用前请求当前任务的知识 bundle。

## 数据流

```text
latest user task
  -> HarnessService.status()
  -> exact Profile digest trusted?
       no  -> no repository body
       yes -> RepositoryKnowledgeIndex
                -> canonical path + exclusion + bounded UTF-8 read
                -> Git HEAD / changed paths
                -> deterministic rank
             -> HarnessKnowledgeContextComposer
                -> L0 manifest
                -> L1 bounded evidence blocks
  -> existing Harness runtime snapshot
  -> AgentEngine._messages only
```

L2 不会自动塞进上下文。Agent 调用 `harness_read_knowledge`，或用户执行 `/harness knowledge` 后，Service 会复用同一索引与安全读取逻辑。

## 信任与缓存

1. 每次知识调用先重新加载 Profile 并读取用户级 Trust Store。
2. 缓存键包含工作区、Profile digest、Git HEAD、changed paths 与文件 digest。
3. 每次命中检查已知候选的大小与 mtime；被复用的 L1 source 还会重新计算精确 bytes digest。
4. Git HEAD/changed paths 最多每 30 秒重新审计一次；NaumiAgent 内成功的写工具会立即使索引失效。
5. 并发 miss 共用一个 build task；相同 snapshot、任务与模型窗口复用最多 16 个进程内选择结果。
6. build 和摘要失配后的重建均再次读取 Profile/Trust；组装期间发生变化时丢弃结果。
7. Trust Store 损坏、Git 不可用或索引失败不会中断主任务，但绝不会降级为“默认信任”。

## 发现与选择

发现顺序遵循：root `AGENTS.md`、目标路径祖先链的嵌套 `AGENTS.md`、Profile entrypoints、构建清单、include 源码/测试。更具体的 `AGENTS.md` 只作用于其目录后代。

排序只使用可复验信号：精确路径、文件名/stem、路径 token、文本/符号命中、import、source-test 配对、Git changed、entrypoint/build fallback。相同得分按 POSIX 相对路径排序，因此相同输入产生相同结果。

## 安全边界

- 不跟随越出工作区的 symlink。
- 不读取目录、设备、socket、无权限、超限或非 UTF-8 文件。
- `.env`、凭据名、私钥后缀、VCS/cache/runtime 目录默认排除。
- 图片、压缩包、日志、完整 diff、二进制和 base64 载荷只保留结构化警告，不进入模型上下文。
- Git 使用 argv、`shell=False` 和 timeout。
- Profile checks 在 H2 只显示，不执行。
