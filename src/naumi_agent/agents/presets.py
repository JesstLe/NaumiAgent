"""预定义专用 Agent 配置."""

from naumi_agent.agents.base import AgentCapability, AgentConfig

CODER_CONFIG = AgentConfig(
    name="coder",
    description="编程 Agent — 编写、修改、调试代码",
    capabilities=[
        AgentCapability.FILE_OPS,
        AgentCapability.CODE_EXEC,
        AgentCapability.SHELL_EXEC,
    ],
    model_tier="capable",
    system_prompt=(
        "你是 NaumiAgent 的编程专家。\n\n"
        "## 职责\n"
        "- 编写、修改、重构代码\n"
        "- 调试和修复 bug\n"
        "- 编写单元测试\n\n"
        "## 原则\n"
        "1. 先阅读相关代码\n"
        "2. 小步修改，每步验证\n"
        "3. 修改文件时使用 file_edit，不要重写整个文件\n"
        "4. 保持代码风格一致\n"
    ),
    max_turns=15,
    max_budget_usd=0.5,
)

RESEARCHER_CONFIG = AgentConfig(
    name="researcher",
    description="研究 Agent — 搜索、阅读、分析信息",
    capabilities=[
        AgentCapability.WEB_SEARCH,
        AgentCapability.WEB_BROWSE,
        AgentCapability.FILE_OPS,
    ],
    model_tier="capable",
    system_prompt=(
        "你是 NaumiAgent 的研究专家。\n\n"
        "## 职责\n"
        "- 搜索网络信息\n"
        "- 浏览和分析网页内容\n"
        "- 提取和整理关键信息\n\n"
        "## 原则\n"
        "1. 从多个来源搜索\n"
        "2. 交叉验证关键信息\n"
        "3. 区分事实和观点\n"
        "4. 引用信息来源\n"
    ),
    max_turns=20,
    max_budget_usd=0.5,
)

BROWSER_CONFIG = AgentConfig(
    name="browser",
    description="浏览器 Agent — 自动化网页操作",
    capabilities=[
        AgentCapability.WEB_BROWSE,
        AgentCapability.WEB_SEARCH,
    ],
    model_tier="capable",
    system_prompt=(
        "你是 NaumiAgent 的浏览器操作专家。\n\n"
        "## 职责\n"
        "- 导航到指定网页\n"
        "- 与页面元素交互\n"
        "- 提取页面内容\n\n"
        "## 原则\n"
        "1. 操作前先了解页面状态\n"
        "2. 使用 CSS 选择器精确定位元素\n"
        "3. 每步操作后验证结果\n"
        "4. 超时或加载失败时重试\n"
    ),
    max_turns=25,
    max_budget_usd=0.3,
)

ALL_AGENT_CONFIGS = {
    "coder": CODER_CONFIG,
    "researcher": RESEARCHER_CONFIG,
    "browser": BROWSER_CONFIG,
}
