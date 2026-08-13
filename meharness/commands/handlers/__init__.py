# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com

from __future__ import annotations

from meharness.commands.handlers.clear import CLEAR_COMMAND
from meharness.commands.handlers.compact import COMPACT_COMMAND
from meharness.commands.handlers.help import HELP_COMMAND
from meharness.commands.handlers.mcp import MCP_COMMAND
from meharness.commands.handlers.memory import MEMORY_COMMAND
from meharness.commands.handlers.permission import PERMISSION_COMMAND
from meharness.commands.handlers.plan import PLAN_COMMAND
from meharness.commands.handlers.session import SESSION_COMMAND
from meharness.commands.handlers.skill import SKILL_COMMAND
from meharness.commands.handlers.rewind import REWIND_COMMAND
from meharness.commands.handlers.status import STATUS_COMMAND
from meharness.commands.registry import CommandRegistry


ALL_COMMANDS = [
    HELP_COMMAND,
    COMPACT_COMMAND,
    CLEAR_COMMAND,
    PLAN_COMMAND,
    SESSION_COMMAND,
    MCP_COMMAND,
    MEMORY_COMMAND,
    PERMISSION_COMMAND,
    REWIND_COMMAND,
    STATUS_COMMAND,
    SKILL_COMMAND,
]


def register_all_commands(registry: CommandRegistry) -> None:
    for cmd in ALL_COMMANDS:
        registry.register_sync(cmd)

