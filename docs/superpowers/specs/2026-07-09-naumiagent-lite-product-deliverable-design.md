# NaumiAgent Lite Product Deliverable Design

## Objective

Create a complete, buyer-facing product deliverable package for a Xiaohongshu-style digital product based on the real NaumiAgent codebase.

The package should help the seller list, promote, and deliver an "engineering-grade Agent project" product. It should also help buyers run the project, understand the architecture, write it on a resume, and answer interview questions.

## Product Positioning

Product name:

`工程级 Agent 项目：从 toy demo 到本地 Coding Agent 系统`

Core promise:

This is not a LangChain wrapper, a shallow RAG customer-service demo, or a prompt-only project. It is a code-grounded local Agent system that can be explained around runtime orchestration, tool calling, memory, permissions, MCP integration, browser automation, and self-evolution.

Target buyers:

- Backend students or junior engineers who need a stronger resume project.
- AI application developers who want to move beyond simple chat/RAG demos.
- Job seekers who need a project they can explain in interviews.
- Builders who want a structured route from Agent demo to Agent engineering.

## Source Grounding

The deliverable should be grounded in these real NaumiAgent capabilities:

- Full-screen CLI entrypoint: `naumi chat`.
- ReAct/orchestrator runtime in `src/naumi_agent/orchestrator/`.
- Tool registry and tool execution in `src/naumi_agent/tools/`.
- Session, long-term memory, and compaction in `src/naumi_agent/memory/`.
- Permission, budget, and guardrail modules in `src/naumi_agent/safety/`.
- MCP client integration in `src/naumi_agent/mcp/`.
- Browser automation and browser daemon tools.
- Task, scheduler, subagent, and pursuit systems.
- Self-review, self-modify, self-evolve, and forge workflows.
- Workbench concepts such as local-first governance, intent locks, decision logs, proposal mode, context health, and validation.

The deliverable must avoid claiming that every advanced future concept is production-ready. It should distinguish between "已实现可演示", "源码中已有雏形", and "推荐二开方向".

## Deliverable Directory

Create all buyer-facing product materials under:

`docs/product/naumiagent-lite/`

Expected files:

- `README.md`
- `商品页文案.md`
- `小红书笔记矩阵.md`
- `课程大纲.md`
- `简历包装.md`
- `面试题库.md`
- `项目讲解稿.md`
- `交付检查清单.md`
- `二开任务路线.md`

## File Responsibilities

### README.md

Buyer entrypoint. It explains what the product is, who it is for, what is included, how to use the materials, how to run the project, and what result the buyer should be able to produce.

It should be friendly, direct, and confidence-building. It should not sound like internal engineering documentation.

### 商品页文案.md

Seller-facing listing copy ready to adapt into a Xiaohongshu goods page.

It should include:

- Product title options.
- Short subtitle.
- Cover image text suggestions.
- Detail-page structure.
- Core selling points.
- Buyer pain points.
- What buyers receive.
- Suggested pricing.
- Purchase notes.
- Digital-product refund boundary.
- Compliance-safe claims.

### 小红书笔记矩阵.md

Content plan for organic traffic.

It should include at least 30 notes, grouped by:

- Pain-point notes.
- Resume/project notes.
- Interview notes.
- Architecture notes.
- Comparison notes.
- Conversion notes.

Each note should include title, opening hook, body outline, and closing call to action.

### 课程大纲.md

Seven-day learning path for buyers.

It should map NaumiAgent concepts into teachable modules:

- Day 1: Project overview and local run.
- Day 2: Agent runtime and ReAct loop.
- Day 3: Tool registry and permissions.
- Day 4: Memory and context compaction.
- Day 5: MCP and browser automation.
- Day 6: self-review, self-modify, forge, pursuit.
- Day 7: resume packaging and interview drill.

### 简历包装.md

Resume-ready project descriptions.

It should include:

- Backend engineer version.
- AI application engineer version.
- Agent engineering version.
- Short, medium, and deep project descriptions.
- Bullet points that are strong but honest.
- Metrics and proof points buyers can replace with their own evidence.

### 面试题库.md

Interview preparation file.

It should include 60 or more Q&A items covering:

- Agent runtime.
- ReAct and planning.
- Tool calling.
- Permission and security.
- Memory and context.
- MCP.
- Browser automation.
- Self-evolution.
- Engineering trade-offs.
- Failure handling.

Answers should be structured and practical rather than inflated.

### 项目讲解稿.md

Speaking scripts for buyers.

It should include:

- 30-second elevator pitch.
- 3-minute interview explanation.
- 8-minute project walkthrough.
- Deep-dive talking points.
- "If challenged by interviewer" response patterns.

### 交付检查清单.md

Quality gate for the product.

It should include seller-side and buyer-side checklists:

- Files exist.
- Project can be run.
- Architecture can be explained.
- Resume text is customized.
- Interview questions have been practiced.
- Claims are honest and evidence-backed.

### 二开任务路线.md

Extension roadmap for buyers.

It should include progressive tasks:

- Easy: add a new deterministic tool.
- Medium: add a new slash command.
- Medium: add a memory export view.
- Hard: add evaluation loop.
- Hard: integrate browser daemon workflow.
- Advanced: implement Workbench governance features.

Each task should include goal, files to inspect, expected output, and verification command.

## Voice And Style

All buyer-facing copy should be Chinese-first.

The tone should be:

- Confident, not exaggerated.
- Practical, not academic.
- Job-seeker friendly, not enterprise-sales heavy.
- Honest about what is implemented versus what is a suggested extension.

Avoid:

- Guaranteed job-offer claims.
- Copying the referenced Xiaohongshu seller's exact wording.
- Pretending every future roadmap item is already production-complete.
- Overusing slogans without concrete code-level evidence.

## Acceptance Criteria

The deliverable is complete when:

- All nine files exist under `docs/product/naumiagent-lite/`.
- The product can be understood from `README.md` alone.
- `商品页文案.md` can be adapted directly into a listing.
- `小红书笔记矩阵.md` contains at least 30 usable note plans.
- `面试题库.md` contains at least 60 question-answer pairs.
- `二开任务路线.md` points to real NaumiAgent directories and verification commands.
- No file contains unfinished placeholder markers.
- The package does not modify application source code.

## Non-Goals

This work will not:

- Build a separate stripped-down source distribution.
- Remove or rewrite existing NaumiAgent code.
- Create image assets or video recordings.
- Make legal, income, or job-offer guarantees.
- Use copied content from any third-party seller page.

## Verification Plan

Because this task is documentation/product packaging, verification should include:

- Check that every expected file exists.
- Search for unfinished placeholder markers.
- Confirm minimum counts for note plans and interview Q&A.
- Confirm no source code files were modified by this package.
- Optionally run `git diff -- docs/product/naumiagent-lite docs/superpowers/specs/2026-07-09-naumiagent-lite-product-deliverable-design.md` for review.
