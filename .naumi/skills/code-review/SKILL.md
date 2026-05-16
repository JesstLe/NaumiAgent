---
name: code-review
description: 代码审查 — 扫描指定路径的代码质量、安全问题、性能隐患
arguments:
  - name: target
    description: 要审查的文件或目录路径
    required: true
  - name: focus
    description: 审查重点 (security/performance/style/all)
    required: false
    default: all
allowed_tools:
  - file_read
  - bash_run
---

# 代码审查 Skill

请对以下目标执行全面的代码审查：

**目标路径**: $ARGUMENTS
**审查重点**: $1

## 当前工作目录

`!`pwd``

## Git 分支信息

`!`git branch --show-current 2>/dev/null || echo "not a git repo"``

## 审查要求

1. **代码质量** — 命名规范、函数长度、重复代码
2. **安全隐患** — SQL 注入、XSS、硬编码密钥、不安全的文件操作
3. **性能问题** — N+1 查询、内存泄漏、不必要的同步操作
4. **错误处理** — 是否覆盖了所有错误路径、错误信息是否清晰
5. **可维护性** — 耦合度、抽象层级、注释质量

对于每个发现的问题，请给出：
- 严重程度（CRITICAL / HIGH / MEDIUM / LOW）
- 文件名和行号
- 问题描述
- 修复建议（含代码示例）

最后给出一个整体评分（A/B/C/D/F）和改进优先级列表。

支持文件参考：${SKILL_DIR}/checklist.yaml
