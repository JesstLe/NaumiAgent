# NaumiAgent Lite Product Deliverable Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a complete Xiaohongshu-style digital product delivery package for NaumiAgent Lite.

**Architecture:** This is a documentation-only product package. All buyer-facing materials live under `docs/product/naumiagent-lite/`, while the implementation plan remains under `docs/superpowers/plans/`. The package reuses real NaumiAgent capabilities as source evidence and does not modify application code.

**Tech Stack:** Markdown documentation, existing NaumiAgent repository docs, shell-based verification.

## Global Constraints

- All buyer-facing copy is Chinese-first.
- The deliverable directory is exactly `docs/product/naumiagent-lite/`.
- The package creates these nine files: `README.md`, `商品页文案.md`, `小红书笔记矩阵.md`, `课程大纲.md`, `简历包装.md`, `面试题库.md`, `项目讲解稿.md`, `交付检查清单.md`, `二开任务路线.md`.
- The package must avoid job-offer, income, or production-readiness guarantees.
- The package must distinguish implemented capabilities, source-level prototypes, and extension directions.
- The package must not modify application source code.

---

### Task 1: Create Buyer Entry, Listing, And Content Matrix

**Files:**
- Create: `docs/product/naumiagent-lite/README.md`
- Create: `docs/product/naumiagent-lite/商品页文案.md`
- Create: `docs/product/naumiagent-lite/小红书笔记矩阵.md`

**Interfaces:**
- Consumes: Product positioning from `docs/superpowers/specs/2026-07-09-naumiagent-lite-product-deliverable-design.md`.
- Produces: Buyer entrypoint, seller listing copy, and at least 30 organic content note plans.

- [ ] **Step 1: Create README**

Write a buyer-facing README that explains the product, who it is for, how to use it, how to run NaumiAgent, and what the buyer should be able to say after learning it.

- [ ] **Step 2: Create listing copy**

Write a Xiaohongshu goods-page-ready file with product title options, cover text, pain points, delivery contents, pricing suggestions, purchase notes, and compliance-safe refund boundary.

- [ ] **Step 3: Create 30-note content matrix**

Write at least 30 note plans grouped by pain, resume, interview, architecture, comparison, and conversion. Each note includes title, hook, body outline, and call to action.

- [ ] **Step 4: Verify Task 1**

Run:

```bash
test -f docs/product/naumiagent-lite/README.md
test -f docs/product/naumiagent-lite/商品页文案.md
test -f docs/product/naumiagent-lite/小红书笔记矩阵.md
rg -n '^## 笔记 ' docs/product/naumiagent-lite/小红书笔记矩阵.md | wc -l
```

Expected: all `test` commands pass and the final count is at least 30.

### Task 2: Create Learning Path, Resume Packaging, And Interview Bank

**Files:**
- Create: `docs/product/naumiagent-lite/课程大纲.md`
- Create: `docs/product/naumiagent-lite/简历包装.md`
- Create: `docs/product/naumiagent-lite/面试题库.md`

**Interfaces:**
- Consumes: Real NaumiAgent source modules listed in the design spec.
- Produces: Seven-day study path, three resume positioning variants, and at least 60 interview Q&A items.

- [ ] **Step 1: Create seven-day course outline**

Write a course outline from local run to Agent runtime, tools, memory, permissions, MCP, browser automation, self-evolution, and interview drill.

- [ ] **Step 2: Create resume packaging**

Write backend, AI application, and Agent engineering resume variants with honest bullets and replaceable evidence metrics.

- [ ] **Step 3: Create interview bank**

Write at least 60 numbered Q&A items. Answers must be practical, source-grounded, and avoid exaggerated claims.

- [ ] **Step 4: Verify Task 2**

Run:

```bash
test -f docs/product/naumiagent-lite/课程大纲.md
test -f docs/product/naumiagent-lite/简历包装.md
test -f docs/product/naumiagent-lite/面试题库.md
rg -n '^## Q[0-9]+' docs/product/naumiagent-lite/面试题库.md | wc -l
```

Expected: all `test` commands pass and the final count is at least 60.

### Task 3: Create Speaking Script, Delivery Checklist, And Extension Roadmap

**Files:**
- Create: `docs/product/naumiagent-lite/项目讲解稿.md`
- Create: `docs/product/naumiagent-lite/交付检查清单.md`
- Create: `docs/product/naumiagent-lite/二开任务路线.md`

**Interfaces:**
- Consumes: Buyer learning path and resume/interview positioning from Tasks 1-2.
- Produces: Speaking scripts, quality gates, and concrete extension tasks with real file paths and verification commands.

- [ ] **Step 1: Create project explanation scripts**

Write 30-second, 3-minute, 8-minute, and deep-dive scripts plus response patterns for skeptical interviewers.

- [ ] **Step 2: Create delivery checklist**

Write seller-side and buyer-side checklists covering product files, local run, screenshots, resume customization, interview practice, and honesty gates.

- [ ] **Step 3: Create extension roadmap**

Write progressive second-development tasks. Each task includes goal, files to inspect, expected output, and verification command.

- [ ] **Step 4: Verify Task 3**

Run:

```bash
test -f docs/product/naumiagent-lite/项目讲解稿.md
test -f docs/product/naumiagent-lite/交付检查清单.md
test -f docs/product/naumiagent-lite/二开任务路线.md
rg -n '^## 任务 ' docs/product/naumiagent-lite/二开任务路线.md | wc -l
```

Expected: all `test` commands pass and the final count is at least 6.

### Task 4: Full Package Verification And Commit

**Files:**
- Verify: `docs/product/naumiagent-lite/`
- Verify: `docs/superpowers/plans/2026-07-09-naumiagent-lite-product-deliverable.md`

**Interfaces:**
- Consumes: All files from Tasks 1-3.
- Produces: Verified docs-only package ready for seller review.

- [ ] **Step 1: Verify expected file count**

Run:

```bash
find docs/product/naumiagent-lite -maxdepth 1 -type f | wc -l
```

Expected: `9`.

- [ ] **Step 2: Verify no unfinished markers**

Run:

```bash
python3 - <<'PY'
from pathlib import Path
markers = ["TB" + "D", "TO" + "DO", "待" + "补充", "FIX" + "ME"]
paths = list(Path("docs/product/naumiagent-lite").glob("*.md"))
paths.append(Path("docs/superpowers/plans/2026-07-09-naumiagent-lite-product-deliverable.md"))
matches = []
for path in paths:
    text = path.read_text(encoding="utf-8")
    for marker in markers:
        if marker in text:
            matches.append(f"{path}: {marker}")
if matches:
    print("\n".join(matches))
    raise SystemExit(1)
PY
```

Expected: no matches.

- [ ] **Step 3: Verify only documentation package files changed**

Run:

```bash
git status --short docs/product/naumiagent-lite docs/superpowers/plans/2026-07-09-naumiagent-lite-product-deliverable.md
```

Expected: only the product package files and plan file appear.

- [ ] **Step 4: Commit docs package only**

Run:

```bash
git add docs/product/naumiagent-lite docs/superpowers/plans/2026-07-09-naumiagent-lite-product-deliverable.md
git commit -m "Add NaumiAgent Lite product deliverable package"
```

Expected: commit succeeds without staging unrelated Mac Workbench changes.
