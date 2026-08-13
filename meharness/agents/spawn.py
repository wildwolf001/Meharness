"""专家运行时：把 AgentCard 装配成完整可运行的 Agent 实例。

关键区别（相对 `.md` 角色过滤）：每个专家拥有**自己的独立工具注册表**
（由 AgentCard.owned_tools 构建，而非从共享注册表过滤），并运行完整的
meharness Agent 引擎（独立 client/上下文/权限沙箱）——能力与主 agent 同级。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from meharness.agents.capability import AgentCard
from meharness.tools import ToolRegistry
from meharness.tools.base import Tool

if TYPE_CHECKING:
    from meharness.agent import Agent
    from meharness.client import LLMClient
    from meharness.permissions import PermissionChecker

log = logging.getLogger(__name__)

# 每个专家都需要的协作工具（P2P 工件流转 + 汇报）
_COLLAB_TOOLS = ["SendMessage", "SyntheticOutput"]


def build_specialist_registry(
    card: AgentCard,
    parent_registry: ToolRegistry,
) -> ToolRegistry:
    """按 AgentCard.owned_tools 构建专家专属注册表。

    从父注册表拉取已正确装配的工具实例（保留 file_cache/file_history/团队
    等构造依赖），只登记该专家拥有的工具 + 协作工具。
    """
    registry = ToolRegistry()
    wanted = set(card.owned_tools) | set(_COLLAB_TOOLS)
    for name in wanted:
        tool: Tool | None = parent_registry.get(name)
        if tool is None:
            log.warning("专家 %s 引用了未注册工具 %s，跳过", card.name, name)
            continue
        registry.register(tool)
    return registry


def build_specialist_agent(
    card: AgentCard,
    *,
    parent_agent: Agent,
    client: LLMClient | None = None,
    work_dir: str | None = None,
    parent_registry: ToolRegistry | None = None,
    permission_checker: PermissionChecker | None = None,
) -> "Agent":
    """把 AgentCard 装配成完整 Agent 实例（能力与主 agent 同级）。

    - 独立注册表：只含 owned_tools
    - 独立权限检查器（默认 dontAsk，契约内全自动）
    - card.prompt 作为系统提示（提示来自卡片，身份来自卡片）
    - 继承父 agent 的 client/协议/上下文窗口/钩子
    """
    from meharness.agent import Agent as AgentClass
    from meharness.permissions import (
        DangerousCommandDetector,
        PathSandbox,
        PermissionChecker,
        PermissionMode,
        RuleEngine,
    )

    registry = build_specialist_registry(
        card,
        parent_registry or parent_agent.registry,
    )

    if permission_checker is None:
        sandbox_dir = work_dir or parent_agent.work_dir
        pm = getattr(PermissionMode, card.permission_mode.upper(), PermissionMode.DONT_ASK)
        permission_checker = PermissionChecker(
            detector=DangerousCommandDetector(),
            sandbox=PathSandbox(sandbox_dir),
            rule_engine=RuleEngine(),
            mode=pm,
        )

    agent = AgentClass(
        client=client or parent_agent.client,
        registry=registry,
        protocol=parent_agent.protocol,
        work_dir=work_dir or parent_agent.work_dir,
        max_iterations=card.max_turns,
        permission_checker=permission_checker,
        context_window=parent_agent.context_window,
        instructions_content=card.prompt,
        hook_engine=parent_agent.hook_engine,
    )
    agent.parent_id = parent_agent.agent_id
    agent.trace_id = parent_agent.trace_id or parent_agent.agent_id
    return agent
