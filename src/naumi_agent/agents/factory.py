"""Dynamic Agent Factory — runtime AgentConfig generation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from naumi_agent.agents.base import DEFAULT_AGENT_MAX_TURNS, AgentCapability, AgentConfig

if TYPE_CHECKING:
    from naumi_agent.runtime.ports.model import ModelPort

logger = logging.getLogger(__name__)

# Task keyword → capability mapping
_CAPABILITY_KEYWORDS: dict[AgentCapability, list[str]] = {
    AgentCapability.FILE_OPS: [
        "file", "read", "write", "edit", "path", "directory",
        "代码", "文件", "读写",
    ],
    AgentCapability.CODE_EXEC: [
        "execute", "run", "compile", "build", "test", "debug",
        "执行", "编译", "测试", "调试",
    ],
    AgentCapability.WEB_SEARCH: [
        "search", "query", "find", "lookup", "investigate",
        "搜索", "查询", "调查",
    ],
    AgentCapability.WEB_BROWSE: [
        "browse", "navigate", "scrape", "crawl", "page", "url",
        "浏览", "网页", "抓取",
    ],
    AgentCapability.SHELL_EXEC: [
        "shell", "bash", "command", "script", "process", "system",
        "命令", "脚本", "系统",
    ],
}

# Domain keywords for system prompt specialization
_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "backend": [
        "api", "server", "database", "sql", "orm", "redis", "cache",
        "queue", "kafka", "grpc", "rest", "endpoint", "migration",
    ],
    "frontend": [
        "ui", "component", "css", "html", "react", "vue", "dom",
        "render", "style", "layout", "responsive", "animation",
    ],
    "infra": [
        "docker", "k8s", "kubernetes", "ci/cd", "terraform", "deploy",
        "nginx", "load.balance", "monitoring", "prometheus", "grafana",
    ],
    "security": [
        "auth", "jwt", "oauth", "encrypt", "decrypt", "ssl", "tls",
        "vulnerability", "xss", "csrf", "inject", "firewall",
    ],
    "data": [
        "etl", "pipeline", "spark", "hadoop", "warehouse", "lake",
        "analytics", "metric", "dashboard", "visualization", "pandas",
    ],
    "ml": [
        "model", "training", "inference", "neural", "transformer",
        "embedding", "vector", "fine.tun", "prompt", "llm", "rag",
    ],
    "architecture": [
        "microservice", "monolith", "event.driven", "cqrs", "ddd",
        "clean.arch", "hexagonal", "design.pattern", "solid",
    ],
}

# Complexity indicators → model tier + budget
_COMPLEXITY_SIGNALS_HIGH = [
    "architect", "design", "refactor", "security.audit", "performance",
    "distributed", "concurrent", "scale", "high.available",
]
_COMPLEXITY_SIGNALS_LOW = [
    "format", "rename", "typo", "lint", "simple", "quick", "minor",
]

# Agent role templates — system prompts for common patterns
_ROLE_TEMPLATES: dict[str, str] = {
    "expert_analyst": (
        "你是一位{domain}领域的顶级专家。\n\n"
        "## 分析焦点\n{focus}\n\n"
        "## 职责\n"
        "从你的专业领域出发，深度分析任务，给出具体可操作的建议。\n\n"
        "## 输出格式\n"
        "### 专家视角\n（你的关键发现）\n"
        "### 具体建议\n（可操作的改进方案）\n"
        "### 风险预警\n（从你的领域看可能出什么问题）\n"
        "### 置信度\n（X/10）"
    ),
    "builder": (
        "你是一个代码建设者。\n\n"
        "## 职责\n"
        "{focus}\n\n"
        "## 原则\n"
        "1. 不走捷径——不能用 try/except 吞掉所有异常来'通过'测试\n"
        "2. 每个防御措施必须有对应的具体攻击场景\n"
        "3. 保持功能完整性——不能为了安全删除核心功能\n"
        "4. 代码必须可运行，不能是伪代码\n\n"
        "## 输出格式\n"
        "给出完整可运行的代码，并附上防御说明。"
    ),
    "attacker": (
        "你是一个代码攻击者（安全审计师）。\n\n"
        "## 职责\n"
        "{focus}\n\n"
        "## 原则\n"
        "1. 基于真实的攻击向量——不能虚无主义式要求绝对安全\n"
        "2. 每个漏洞必须给出具体的攻击示例（输入/场景）\n"
        "3. 区分 CRITICAL / HIGH / MEDIUM / LOW 严重级别\n"
        "4. 关注功能正确性，不仅仅是安全\n\n"
        "## 输出格式\n"
        "逐条列出发现的漏洞，每条包含：攻击向量、严重级别、利用方式、修复建议。"
    ),
    "reviewer": (
        "你是一个代码审查专家。\n\n"
        "## 职责\n"
        "{focus}\n\n"
        "## 审查要点\n"
        "1. 功能正确性：代码是否真正解决了问题？\n"
        "2. 边界情况：空输入、超大数据、特殊字符是否处理？\n"
        "3. 代码质量：可读性、可维护性、是否符合最佳实践？\n"
        "4. 安全性：是否存在注入、泄漏、权限绕过？\n\n"
        "如果一切良好，回复 APPROVED。\n"
        "如果有问题，给出具体修改建议。"
    ),
    "worker": (
        "你是 Supervisor 树中的 Worker 节点。\n\n"
        "## Let-it-crash 原则\n"
        "- 你被允许失败。遇到异常直接抛出，不要 try/except 吞掉。\n"
        "- 你的任务是分析目标，识别所有崩溃点和恢复策略。\n\n"
        "## 职责\n{focus}\n\n"
        "## 输出格式\n"
        "对每个崩溃点输出:\n"
        "- 位置（函数/行号）\n"
        "- 触发条件\n"
        "- 崩溃传播范围\n"
        "- 建议的恢复策略（重启/降级/回滚）"
    ),
    "guardian": (
        "你是 Supervisor 树中的 Guardian 节点。\n\n"
        "## 职责\n"
        "- 审查 Worker 的分析结果\n"
        "- 设计 Erlang 式的 Supervisor 树结构\n"
        "- 定义每层级的重启策略\n"
        "- 规划回滚优先级\n\n"
        "## 原则\n{focus}\n\n"
        "- 权限不对称：Guardian 不能被 Worker 的异常影响\n"
        "- 回滚优先于调试\n"
        "- 隔离爆炸半径"
    ),
}

# LLM prompt for generating specialized system prompts
_PROMPT_GENERATION_SYSTEM = """\
You are a system prompt generator for specialized AI agents.

Given a task description and agent role, generate a concise, focused system prompt.
The prompt should:
1. Define the agent's expertise domain clearly
2. Specify exact responsibilities and scope
3. Include output format requirements
4. Set behavioral boundaries (what NOT to do)

Output ONLY the system prompt text, nothing else. No markdown, no explanation.
Keep it under 500 words. Use Chinese for all user-facing text.
"""


class DynamicAgentFactory:
    """Create AgentConfig dynamically based on task requirements."""

    def __init__(self, router: ModelPort) -> None:
        self._router = router

    def create_config(
        self,
        *,
        name: str,
        task_description: str,
        role: str = "expert_analyst",
        focus: str = "",
        domain: str = "",
        model_tier: str | None = None,
        max_turns: int | None = None,
        max_budget_usd: float | None = None,
        extra_capabilities: list[AgentCapability] | None = None,
    ) -> AgentConfig:
        """Create an AgentConfig based on task analysis.

        Args:
            name: Unique agent name (used for lifecycle management).
            task_description: The task this agent will handle.
            role: Predefined role template (expert_analyst, builder,
                attacker, reviewer, worker, guardian).
            focus: Specific focus area — overrides template default.
            domain: Domain specialization — auto-detected if empty.
            model_tier: Override auto-detected tier.
            max_turns: Override default turn limit.
            max_budget_usd: Override default budget.
            extra_capabilities: Additional capabilities beyond auto-detection.

        """
        lower_task = task_description.lower()

        # Detect capabilities from task description
        capabilities = self._detect_capabilities(lower_task)
        if extra_capabilities:
            for cap in extra_capabilities:
                if cap not in capabilities:
                    capabilities.append(cap)

        # Ensure at least FILE_OPS for code analysis tasks
        if not capabilities:
            capabilities.append(AgentCapability.FILE_OPS)

        # Detect domain
        detected_domain = domain or self._detect_domain(lower_task)
        if not focus:
            focus = f"{detected_domain}领域的深度分析与方案设计"

        # Generate system prompt
        system_prompt = self._build_system_prompt(role, detected_domain, focus)

        # Determine model tier while preserving shared runtime limits.
        if model_tier is None:
            model_tier = self._detect_tier(lower_task)
        if max_turns is None:
            max_turns = DEFAULT_AGENT_MAX_TURNS

        config = AgentConfig(
            name=name,
            description=f"{detected_domain or '通用'}专家 — {focus[:60]}",
            capabilities=capabilities,
            model_tier=model_tier,
            system_prompt=system_prompt,
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
        )

        logger.info(
            "Created dynamic config: %s (domain=%s, tier=%s, caps=%s)",
            name, detected_domain, model_tier,
            [c.value for c in capabilities],
        )
        return config

    async def create_config_with_llm_prompt(
        self,
        *,
        name: str,
        task_description: str,
        role: str = "expert_analyst",
        focus: str = "",
        domain: str = "",
        model_tier: str | None = None,
        max_turns: int | None = None,
        max_budget_usd: float | None = None,
        extra_capabilities: list[AgentCapability] | None = None,
    ) -> AgentConfig:
        """Create AgentConfig with LLM-generated system prompt.

        Same as create_config but uses the LLM to generate a specialized
        system prompt instead of using template substitution.
        """
        config = self.create_config(
            name=name,
            task_description=task_description,
            role=role,
            focus=focus,
            domain=domain,
            model_tier=model_tier,
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
            extra_capabilities=extra_capabilities,
        )

        # Generate specialized prompt via LLM
        llm_prompt = await self._generate_prompt_via_llm(
            task_description, role, config.description,
        )

        # Return new config with LLM-generated prompt (AgentConfig is frozen)
        return AgentConfig(
            name=config.name,
            description=config.description,
            capabilities=config.capabilities,
            model_tier=config.model_tier,
            system_prompt=llm_prompt,
            max_turns=config.max_turns,
            max_budget_usd=config.max_budget_usd,
            tools=config.tools,
            permission_level=config.permission_level,
        )

    def _detect_capabilities(self, text: str) -> list[AgentCapability]:
        """Infer required capabilities from task text."""
        caps: list[AgentCapability] = []
        for cap, keywords in _CAPABILITY_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                caps.append(cap)
        return caps

    def _detect_domain(self, text: str) -> str:
        """Detect the primary domain from task text."""
        best_domain = "通用"
        best_count = 0
        for domain, keywords in _DOMAIN_KEYWORDS.items():
            count = sum(text.count(kw) for kw in keywords)
            if count > best_count:
                best_domain = domain
                best_count = count
        return best_domain

    def _detect_tier(self, text: str) -> str:
        """Select model tier based on task complexity signals."""
        if any(sig in text for sig in _COMPLEXITY_SIGNALS_HIGH):
            return "reasoning"
        if any(sig in text for sig in _COMPLEXITY_SIGNALS_LOW):
            return "fast"
        return "capable"

    def _build_system_prompt(
        self, role: str, domain: str, focus: str,
    ) -> str:
        """Build system prompt from role template."""
        template = _ROLE_TEMPLATES.get(role, _ROLE_TEMPLATES["expert_analyst"])
        return template.format(domain=domain, focus=focus)

    async def _generate_prompt_via_llm(
        self, task: str, role: str, agent_desc: str,
    ) -> str:
        """Use LLM to generate a specialized system prompt."""
        from naumi_agent.model.router import ModelTier

        try:
            user_msg = (
                f"## Task Description\n{task}\n\n"
                f"## Agent Role\n{role}\n\n"
                f"## Agent Description\n{agent_desc}\n\n"
                "Generate the system prompt for this agent."
            )
            response = await self._router.call(
                messages=[
                    {"role": "system", "content": _PROMPT_GENERATION_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                tier=ModelTier.FAST,
                max_tokens=800,
                temperature=1.0,
            )
            return response.content.strip()
        except Exception as e:
            logger.warning("LLM prompt generation failed: %s", e)
            return f"你是一个专业分析 Agent。任务: {task[:200]}"
