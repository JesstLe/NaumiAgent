# Skill 示例

## 目录结构

每个 Skill 是一个目录，核心是 `SKILL.md` 文件：

```
.naumi/skills/          # 运行时搜索路径（项目级）
├── code-review/
│   └── SKILL.md        # 必须存在
├── deploy-check/
│   ├── SKILL.md
│   └── checklist.yaml  # 可选支持文件（通过 ${SKILL_DIR} 引用）
└── ...

~/.naumi/skills/        # 运行时搜索路径（用户级，全局共享）
└── ...
```

## 安装示例

将 `docs/examples/skills/` 下的示例复制到项目的 `.naumi/skills/` 即可：

```bash
mkdir -p .naumi/skills/
cp -r docs/examples/skills/code-review .naumi/skills/
```

## 调用方式

### CLI 斜杠命令

```
/code-review src/naumi_agent/skills/ security
```

### LLM 自主调用

Agent 会在推理链中看到 `skill_code-review` 工具，可自主决策何时调用。

## 编写自己的 Skill

参考 `code-review/SKILL.md` 的格式，创建一个新目录并编写 `SKILL.md`：

```yaml
---
name: my-skill
description: 一句话描述
arguments:
  - name: target
    description: 参数说明
    required: true
allowed_tools:
  - file_read
---

# 指令模板

对 $ARGUMENTS 执行操作。
第一个参数: $0
Skill 目录: ${SKILL_DIR}
动态信息: `!`uname -s``
```

### 模板变量

| 变量 | 说明 |
|------|------|
| `$ARGUMENTS` | 完整参数字符串 |
| `$0`, `$1`, ... | 按空格拆分的位置参数 |
| `${SKILL_DIR}` | Skill 所在目录的绝对路径 |
| `!`command`` | 动态上下文注入，替换为命令的 stdout |
| `${name}` | 自定义变量，通过 `extra_vars` 传入 |

### 搜索路径

按优先级从高到低：

1. `<project>/.naumi/skills/` — 项目级
2. `~/.naumi/skills/` — 用户级（全局共享）
3. `config.yaml` 中 `skills.search_paths` 指定的额外路径
