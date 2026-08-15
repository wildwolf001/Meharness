# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
"""ReplApp —— 行流式 TUI 控制器（替代 Textual MeharnessApp）。

用 LineStream 做顺序追加 + 块内流式渲染；自研 raw 按键输入；
保留 agent 事件流、命令系统、权限/AskUser/计划审批的异步契约。
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time as _time
from pathlib import Path
from typing import Any

from meharness.agent import (
    Agent,
    CompactNotification,
    ErrorEvent,
    HookEvent,
    LoopComplete,
    PermissionRequest,
    PermissionResponse,
    RetryEvent,
    StreamText,
    ThinkingText,
    ToolResultEvent,
    ToolUseEvent,
    TurnComplete,
)
from meharness.client import (
    AuthenticationError,
    LLMClient,
    create_client,
    resolve_context_window,
)
from meharness.commands import CommandRegistry, complete, parse_command
from meharness.commands.registry import CommandContext
from meharness.commands.handlers import register_all_commands
from meharness.config import MCPServerConfig, ProviderConfig, WorktreeConfig
from meharness.conversation import ConversationManager, Message
from meharness.hooks import HookEngine
from meharness.memory import (
    MemoryManager,
    SessionManager,
    find_relevant_memories,
    load_instructions,
    render_reminder,
)
from meharness.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from meharness.term.ansi import (
    DIM,
    FG_BRIGHT_CYAN,
    FG_GREEN,
    FG_RED,
    FG_YELLOW,
    ITALIC,
    RESET,
    bg256,
    fg256,
    styled,
)
from meharness.term.keys import KeyReader, MouseEvent
from meharness.term.overlays import (
    AskUserOverlay,
    BannerOverlay,
    CommandCompletionOverlay,
    ListOverlay,
    PermissionOverlay,
    PlanOverlay,
    TextInputOverlay,
    match_commands,
    tool_verb,
)
from meharness.term.screen import FullscreenScreen, Overlay, Seg, cell_width as _cell_width
from meharness.agents.notification import inject_task_notifications
from meharness.tools import create_default_registry
from meharness.tools.ask_user import AskUserEvent
from meharness.ui.clipboard import set_clipboard
from meharness.ui.selection import SelectionState, extract_selected_text

_MAX_INPUT = 4000

# 工具执行 spinner 帧（对齐 claude：执行期间动词转帧）
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


# ---------------------------------------------------------------------------
# 渲染辅助（从 app.py 复制的小工具，避免引入 Textual）
# ---------------------------------------------------------------------------

_MODE_CYCLE = [
    PermissionMode.DEFAULT,
    PermissionMode.ACCEPT_EDITS,
    PermissionMode.PLAN,
    PermissionMode.BYPASS,
]
_MODE_LABELS = {
    PermissionMode.DEFAULT: "default",
    PermissionMode.ACCEPT_EDITS: "accept-edits",
    PermissionMode.PLAN: "plan",
    PermissionMode.BYPASS: "yolo",
}
_MODE_COLORS = {
    PermissionMode.DEFAULT: DIM,
    PermissionMode.ACCEPT_EDITS: FG_GREEN,
    PermissionMode.PLAN: FG_YELLOW,
    PermissionMode.BYPASS: FG_RED,
}


def _fmt_k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _display_col(text: str) -> int:
    """文本的终端格宽（CJK=2 格）。"""
    return sum(_cell_width(ch) for ch in text)


def _assistant_segs(text: str) -> list[Seg]:
    """assistant 流式块：● 品牌前缀 + 正文。"""
    return [Seg("● ", fg=fg256(173)), Seg(text)]


def _wrap_lines(text: str, width: int) -> list[str]:
    """把响应文本按终端宽度折行（保留显式换行），返回行列表。"""
    if width <= 0:
        width = 60
    lines: list[str] = []
    for raw in text.split("\n"):
        if not raw:
            lines.append("")
            continue
        buf: list[str] = []
        used = 0
        for ch in raw:
            w = _cell_width(ch)
            if used + w > width and buf:
                lines.append("".join(buf))
                buf = []
                used = 0
            buf.append(ch)
            used += w
        lines.append("".join(buf))
    return lines


# ---------------------------------------------------------------------------
# ReplApp
# ---------------------------------------------------------------------------


class ReplApp:
    def __init__(
        self,
        providers: list[ProviderConfig],
        permission_mode: PermissionMode = PermissionMode.DEFAULT,
        mcp_servers: list[MCPServerConfig] | None = None,
        hook_engine: HookEngine | None = None,
        enable_fork: bool = False,
        enable_verification_agent: bool = False,
        worktree_config: WorktreeConfig | None = None,
        teammate_mode: str = "",
        enable_coordinator_mode: bool = False,
    ) -> None:
        self.providers = providers
        self._initial_permission_mode = permission_mode
        self._mcp_server_configs = mcp_servers or []
        self.hook_engine = hook_engine
        self._enable_fork = enable_fork
        self._enable_verification_agent = enable_verification_agent
        self._worktree_config = worktree_config
        self._teammate_mode = teammate_mode
        self._enable_coordinator_mode = enable_coordinator_mode

        self._selection = SelectionState()
        self.stream = FullscreenScreen(selection=self._selection)
        self.conversation = ConversationManager()
        self.registry = create_default_registry()
        self.agent: Agent | None = None
        self.client: LLMClient | None = None
        self._selected_provider: ProviderConfig | None = None
        self.session_manager: SessionManager | None = None
        self.session = None
        self.memory_manager: MemoryManager | None = None
        self._instructions_content = ""
        self.skill_loader = None
        self.skill_executor = None
        self.agent_loader = None
        self.task_manager = None
        self.trace_manager = None
        self.worktree_manager = None
        self.team_manager = None

        self.command_registry = CommandRegistry()
        register_all_commands(self.command_registry)

        # 输入状态
        self._input_text = ""
        self._input_cursor = 0
        self._pending_sends: list[str] = []  # 响应期间排队的消息（claude 式）
        self._history: list[str] = []
        self._history_index = -1
        self._history_draft = ""
        self._keys: asyncio.Queue = asyncio.Queue()
        self._quit = False
        self._streaming = False
        self._agent_task: asyncio.Task | None = None
        self._thinking_accum = ""
        self._thinking_start = 0.0
        self._thinking_verb = "Working"

        self._spinner_timer: asyncio.Task | None = None

        # 工具执行 spinner（claude 式：动词 + 转帧）
        self._active_tool_blocks: dict[str, tuple[str, str]] = {}  # tool_id -> (block_id, verb)
        self._tool_spinner_timer: asyncio.Task | None = None
        self._tool_spinner_frame = 0
        self._token_warned = False

        # 记忆去重 + 近期工具（对齐 claude sideQuery 的 already_surfaced/recent_tools）
        self._surfaced_memories: set[str] = set()
        self._recent_tools: list[str] = []
        # 记忆预取防抖：同一时刻至多一个 side LLM 请求在跑，避免与主响应抢连接
        self._prefetch_inflight = False

    # ------------------------------------------------------------------
    # 运行入口
    # ------------------------------------------------------------------

    async def run(self) -> None:
        from meharness.term.ansi import setup_utf8

        setup_utf8()
        self._start_time = _time.monotonic()
        self.stream.start()
        self.stream.commit_text(
            styled(" Meharness v0.1.0", FG_BRIGHT_CYAN, "1")
        )
        self._update_status()
        if not self.providers:
            self.stream.commit_text("  ✖ 未配置 provider，请检查 config.yaml")
            self.stream.restore()
            return
        try:
            self._setup(self.providers[0])
        except Exception as e:
            self.stream.commit_text(f"  ✖ 初始化失败: {e}")
            self.stream.restore()
            return
        if self.agent is None:
            self.stream.commit_text("  ✖ 初始化失败")
            self.stream.restore()
            return

        self.stream.commit_text(f"  model: {self._selected_provider.model}")
        self._render_input()

        # 会话启动记忆预取（对齐 claude：首条消息前就 prefetch，后台非阻塞预热）
        if self.memory_manager is not None and self._selected_provider is not None:
            asyncio.create_task(self._session_prefetch())

        loop = asyncio.get_running_loop()
        threading.Thread(target=self._key_pump, args=(loop,), daemon=True).start()

        poll_task = asyncio.create_task(self._notification_poll())
        try:
            while not self._quit:
                key = await self._keys.get()
                await self._handle_key(key)
        finally:
            poll_task.cancel()
            await self._shutdown()

    def _key_pump(self, loop: asyncio.AbstractEventLoop) -> None:
        kr = KeyReader()
        try:
            while True:
                k = kr.read_key()
                try:
                    loop.call_soon_threadsafe(self._keys.put_nowait, k)
                except Exception:
                    break
        except Exception:
            pass
        finally:
            try:
                kr.restore_terminal()
            except Exception:
                pass

    async def _shutdown(self) -> None:
        try:
            self.stream.restore()
            if self._spinner_timer:
                self._spinner_timer.cancel()
            if self.agent and self.agent.memory_manager:
                try:
                    await asyncio.wait_for(
                        self.agent._extract_memories(self.conversation), timeout=3.0
                    )
                except Exception:
                    pass
            if self.session:
                self.session.close()
        except Exception:
            pass

    async def _notification_poll(self) -> None:
        """后台任务完成轮询：队友结束后置 idle 并通知 lead。

        对齐 Textual app.py 的 _start_notification_polling/_process_task_notifications；
        ReplApp 是按键驱动，轮询只在非 streaming 时处理，避免打断当前 agent.run。
        """
        while not self._quit:
            await asyncio.sleep(2)
            if self._streaming or self.agent is None:
                continue
            try:
                await self._process_task_notifications()
            except Exception:
                pass

    async def _process_task_notifications(self) -> None:
        if self.task_manager is None or self.agent is None:
            return
        completed = self.task_manager.poll_completed()
        if not completed:
            return
        inject_task_notifications(self.conversation, completed)
        for task in completed:
            status_icon = "✓" if task.status == "completed" else "✗"
            self.add_system_message(
                f"{status_icon} 后台任务完成: [{task.id}] {task.name} — {task.status}"
            )
            if self.team_manager is not None:
                self.team_manager.on_teammate_completed(task.agent.agent_id)
        # 触发 agent 消化注入的任务通知
        self._agent_task = asyncio.create_task(
            self._send_message("", is_notification=True)
        )

    # ------------------------------------------------------------------
    # 初始化（对齐 MeharnessApp._select_provider 的非 UI 部分）
    # ------------------------------------------------------------------

    def _setup(self, provider: ProviderConfig) -> None:
        self._selected_provider = provider
        try:
            self.client = create_client(provider)
        except AuthenticationError as e:
            self.stream.commit_text(f"  ✖ {e}")
            return

        work_dir = os.getcwd()
        home = Path.home()
        checker = PermissionChecker(
            detector=DangerousCommandDetector(),
            sandbox=PathSandbox(work_dir),
            rule_engine=RuleEngine(
                user_rules_path=home / ".meharness" / "permissions.yaml",
                project_rules_path=Path(work_dir) / ".meharness" / "permissions.yaml",
                local_rules_path=Path(work_dir) / ".meharness" / "permissions.local.yaml",
            ),
            mode=self._initial_permission_mode,
        )

        self._instructions_content = load_instructions(work_dir)
        self.memory_manager = MemoryManager(work_dir)
        self.session_manager = SessionManager(work_dir)
        self.session_manager.cleanup()
        self.session = self.session_manager.create()

        from meharness.filehistory import FileHistory
        from meharness.skills.executor import SkillExecutor
        from meharness.skills.loader import SkillLoader
        from meharness.tools.ask_user import AskUserTool
        from meharness.tools.exit_plan_mode import ExitPlanModeTool
        from meharness.tools.impl.tool_search import ToolSearchTool
        from meharness.tools.load_skill import LoadSkill
        from meharness.tools.run_pipeline import RunPaperPipelineTool
        from meharness.worktree.manager import WorktreeManager

        file_history = FileHistory(work_dir, self.session.session_id)
        for tool in self.registry.list_tools():
            if hasattr(tool, "file_history"):
                tool.file_history = file_history

        self.registry.register(LoadSkill())
        self.registry.register(ToolSearchTool(self.registry, protocol=provider.protocol))
        self.registry.register(AskUserTool())
        self.registry.register(ExitPlanModeTool())

        self.agent = Agent(
            client=self.client,
            registry=self.registry,
            protocol=provider.protocol,
            work_dir=work_dir,
            permission_checker=checker,
            context_window=provider.get_context_window(),
            instructions_content=self._instructions_content,
            memory_manager=self.memory_manager,
            hook_engine=self.hook_engine,
        )
        self.agent.file_history = file_history
        self.agent.session_id = self.session.session_id

        self.registry.register(RunPaperPipelineTool(self.agent))

        # 后台拉取 context window（尽力而为）
        asyncio.ensure_future(self._resolve_context_window(provider))

        # skills
        self.skill_loader = SkillLoader(work_dir)
        self.skill_loader.load_all()
        catalog = self.skill_loader.get_catalog()
        if catalog:
            lines = ["You can use the following Skills:", ""]
            for name, desc in catalog:
                lines.append(f"- {name}: {desc}")
            lines.append("")
            lines.append(
                "If the user's request matches a Skill, call LoadSkill to activate it."
            )
            self.agent.set_skill_catalog("\n".join(lines))

        self.skill_executor = SkillExecutor(
            agent=self.agent, client=self.client, protocol=provider.protocol
        )

        # worktree
        wt_cfg = self._worktree_config or WorktreeConfig()
        self.worktree_manager = WorktreeManager(
            repo_root=work_dir,
            symlink_directories=wt_cfg.symlink_directories,
        )
        restored = self.worktree_manager.restore_session()
        if restored:
            self.agent.work_dir = restored.worktree_path

        # worktree 工具 + /worktree 命令（对齐 claude，旧 Textual app.py:831-837）
        from meharness.tools.enter_worktree import EnterWorktreeTool
        from meharness.tools.exit_worktree import ExitWorktreeTool
        from meharness.commands.handlers.worktree import create_worktree_command

        self.registry.register(
            EnterWorktreeTool(worktree_manager=self.worktree_manager)
        )
        self.registry.register(
            ExitWorktreeTool(worktree_manager=self.worktree_manager)
        )
        self.command_registry.register_sync(
            create_worktree_command(self.worktree_manager)
        )

        # 子 agent + 团队
        from meharness.agents.loader import AgentLoader
        from meharness.agents.task_manager import TaskManager
        from meharness.agents.trace import TraceManager
        from meharness.teams.manager import TeamManager
        from meharness.tools.agent_tool import AgentTool
        from meharness.tools.team_create import TeamCreateTool
        from meharness.tools.team_delete import TeamDeleteTool
        from meharness.worktree.cleanup import start_stale_cleanup_task

        self.agent_loader = AgentLoader(
            work_dir, enable_verification=self._enable_verification_agent
        )
        self.agent_loader.load_all()
        self.trace_manager = TraceManager()
        self.task_manager = TaskManager()

        # /tasks + /trace 命令（对齐 claude，旧 Textual app.py:913-918）
        from meharness.commands.handlers.tasks import create_tasks_command
        from meharness.commands.handlers.trace import create_trace_command

        self.command_registry.register_sync(
            create_tasks_command(self.task_manager)
        )
        self.command_registry.register_sync(
            create_trace_command(self.trace_manager, self.agent.agent_id)
        )
        self.team_manager = TeamManager(
            worktree_manager=self.worktree_manager,
            trace_manager=self.trace_manager,
        )

        self.registry.register(
            AgentTool(
                agent_loader=self.agent_loader,
                task_manager=self.task_manager,
                trace_manager=self.trace_manager,
                parent_agent=self.agent,
                enable_fork=self._enable_fork,
                provider_config=provider,
                worktree_manager=self.worktree_manager,
                team_manager=self.team_manager,
            )
        )
        self.registry.register(
            TeamCreateTool(
                team_manager=self.team_manager,
                parent_agent=self.agent,
                teammate_mode=self._teammate_mode,
                is_interactive=True,
                enable_coordinator_mode=self._enable_coordinator_mode,
            )
        )
        self.registry.register(TeamDeleteTool(team_manager=self.team_manager, parent_agent=self.agent))

        agent_catalog = self.agent_loader.list_agents()
        if agent_catalog:
            lines = [
                "## Available Sub-Agent Types",
                "Use the Agent tool with subagent_type parameter to delegate tasks:",
                "",
            ]
            for agent_type, when_to_use in agent_catalog:
                lines.append(f"- **{agent_type}**: {when_to_use}")
            lines.append("")
            lines.append(
                "IMPORTANT: Sub-agents run in the background. "
                "After calling the Agent tool, you will get a task ID immediately. "
                "Do NOT wait, sleep, or poll. Report the task ID to the user and end your turn. "
                "The system will automatically notify when the task completes."
            )
            self.agent.set_agent_catalog("\n".join(lines), catalog_list=agent_catalog)

        from meharness.tools.synthetic_output import SyntheticOutputTool

        self.registry.register(SyntheticOutputTool())
        self.agent._team_manager = self.team_manager
        self.agent.notification_fn = lambda: self.team_manager.drain_lead_mailbox()

        asyncio.ensure_future(self._init_mcp(provider))

    async def _resolve_context_window(self, provider: ProviderConfig) -> None:
        await resolve_context_window(provider)
        if self.agent is not None:
            self.agent.context_window = provider.get_context_window()

    async def _init_mcp(self, provider: ProviderConfig) -> None:
        if not self._mcp_server_configs:
            return
        try:
            from meharness.mcp import MCPManager

            manager = MCPManager()
            manager.load_configs(self._mcp_server_configs)
            await manager.register_all_tools(self.registry)
        except Exception as e:
            self.stream.commit_text(f"  MCP warning: {e}")

    # ------------------------------------------------------------------
    # UIController（命令系统契约）
    # ------------------------------------------------------------------

    def add_system_message(self, text: str) -> None:
        self.stream.commit_text("  " + text)

    def send_user_message(self, text: str) -> None:
        if self._streaming or self.agent is None:
            return
        self._agent_task = asyncio.create_task(self._send_message(text))

    def _update_status(self, render: bool = True) -> None:
        """底部状态栏：模式 · token in/out · 会话时长。"""
        if self.agent is None:
            self.stream.set_status("", render=render)
            return
        mode = self.agent.permission_mode
        label = _MODE_LABELS.get(mode, mode.value if mode else "?")
        elapsed = int(_time.monotonic() - self._start_time) if getattr(self, "_start_time", None) else 0
        in_k = _fmt_k(self.agent.total_input_tokens)
        out_k = _fmt_k(self.agent.total_output_tokens)
        self.stream.set_status(f"{label} · in {in_k} · out {out_k} · {elapsed}s", render=render)

        # 接近上下文窗口时一次性告警横幅（对齐 claude TokenWarning）
        if not self._token_warned and self.agent.total_input_tokens > int(
            (self.agent.context_window or 200_000) * 0.8
        ):
            self._token_warned = True
            self._push_banner(
                f"上下文使用已超过 80%（{_fmt_k(self.agent.total_input_tokens)} / "
                f"{_fmt_k(self.agent.context_window or 200_000)}）",
                FG_YELLOW,
            )

    def refresh_status(self) -> None:
        self._update_status(render=False)
        self._render_input()

    def get_token_count(self) -> tuple[int, int]:
        if self.agent:
            return self.agent.total_input_tokens, self.agent.total_output_tokens
        return 0, 0

    def set_plan_mode(self, enabled: bool) -> None:
        if self.agent is None:
            return
        if enabled:
            self._pre_plan_mode = self.agent.permission_mode
            self.agent.set_permission_mode(PermissionMode.PLAN)
        else:
            restore = getattr(self, "_pre_plan_mode", PermissionMode.DEFAULT)
            self.agent.set_permission_mode(restore)

    # ------------------------------------------------------------------
    # 输入渲染
    # ------------------------------------------------------------------

    def _mode_prefix(self) -> str:
        if self.agent is None:
            return ""
        m = self.agent.permission_mode
        if m == PermissionMode.DEFAULT:
            return "❯ "
        label = _MODE_LABELS.get(m, m.value)
        return f"❯[{label}] "

    def _render_input(self) -> None:
        # 只渲染一次：状态栏 + 输入行合并（set_status 不渲染，set_input 渲染）
        self._update_status(render=False)
        # 完整文本交给 screen 做多行折行（不在此截断单行）
        # 输入框 prompt 与 claude 一致为 "❯ "；权限模式只在底部状态栏显示
        self.stream.set_input(
            self._input_text, cursor=self._input_cursor, prompt="❯ "
        )

    def _input_col(self) -> int:
        prompt = self._mode_prefix()
        return _display_col(prompt) + _display_col(self._input_text[: self._input_cursor])

    # ------------------------------------------------------------------
    # 按键处理
    # ------------------------------------------------------------------

    async def _handle_key(self, key) -> None:
        # overlay 栈优先：从栈顶向下找第一个消费按键的 overlay。
        # - result 完成 → remove_overlay；若为斜杠补全面板，执行选中命令。
        # - modal overlay 吞掉未消费的键；非 modal（补全面板 / banner）允许
        #   未消费的键落到下层 overlay 或输入编辑。
        ov = self.stream.top_overlay()
        if ov is not None:
            # 从栈顶向下走：第一个消费按键的 overlay 赢；modal overlay 吞掉
            # 未消费的键；非 modal（banner 等）不消费则让键落到下层。
            o = ov
            while o is not None:
                consumed = await o.on_key(key)
                result = getattr(o, "result", None)
                if result is not None and result.done():
                    self.stream.remove_overlay(o)
                    if isinstance(o, CommandCompletionOverlay):
                        await self._run_command_panel(o, result.result())
                    return
                if consumed:
                    self.stream.render()
                    return
                if getattr(o, "modal", True):
                    return
                o = self.stream.overlay_below(o)
        if key[0] == "mouse":
            self._handle_mouse(key[1])
            return
        if self._streaming:
            if key == ("ctrl_c",) or key == ("escape",):
                # Esc 也是中断（对齐 claude-code：Esc 打断当前响应）。
                # 仅在输入框为空时中断，避免误伤正在编辑的内容。
                if key == ("escape",) and self._input_text:
                    self._input_text = ""
                    self._input_cursor = 0
                    self._render_input()
                    return
                await self._interrupt()
                return
            # 响应期间输入框保持可编辑（claude 式）：编辑不打断流式，
            # Enter 则排队，当前响应完成后自动发送。
            kind = key[0]
            if kind == "print":
                self._insert_text(key[1])
            elif kind == "enter":
                self._queue_send()
            elif kind == "backspace":
                self._backspace()
            elif kind == "delete":
                self._delete()
            elif kind == "left":
                if self._input_cursor > 0:
                    self._input_cursor -= 1
                    self._render_input()
            elif kind == "right":
                if self._input_cursor < len(self._input_text):
                    self._input_cursor += 1
                    self._render_input()
            elif kind == "home":
                self._input_cursor = 0
                self._render_input()
            elif kind == "end":
                self._input_cursor = len(self._input_text)
                self._render_input()
            elif kind == "tab":
                await self._tab_complete()
            return
        if key == ("eof",):
            self._quit = True
            return
        kind = key[0]
        if kind == "print":
            self._insert_text(key[1])
        elif kind == "enter":
            await self._submit()
        elif kind == "backspace":
            self._backspace()
        elif kind == "delete":
            self._delete()
        elif kind == "page_up":
            self.stream.scroll(-(self.stream._content_height() // 2))
        elif kind == "page_down":
            self.stream.scroll(self.stream._content_height() // 2)
        elif kind == "ctrl_c":
            if self._selection.active:
                self._copy_selection()
            else:
                await self._interrupt()
        elif kind == "left":
            if self._input_cursor > 0:
                self._input_cursor -= 1
                self._render_input()
        elif kind == "right":
            if self._input_cursor < len(self._input_text):
                self._input_cursor += 1
                self._render_input()
        elif kind == "home":
            self._input_cursor = 0
            self._render_input()
        elif kind == "end":
            self._input_cursor = len(self._input_text)
            self._render_input()
        elif kind == "up":
            self._history_nav(-1)
        elif kind == "down":
            self._history_nav(1)
        elif kind == "tab":
            await self._tab_complete()
        elif kind == "shift_tab":
            await self._cycle_mode()
        elif kind == "ctrl_o":
            self._toggle_expand()
        elif kind == "escape":
            if self._input_text:
                self._input_text = ""
                self._input_cursor = 0
                self._render_input()
                self._sync_command_panel()

    def _handle_mouse(self, ev: MouseEvent) -> None:
        """内容区拖拽选区 / 滚轮滚动。坐标换算：屏幕行 y → buffer 行。"""
        content_h = self.stream._content_height()
        if ev.button == "wheel_up":
            self.stream.scroll(-3)
            return
        if ev.button == "wheel_down":
            self.stream.scroll(3)
            return
        if ev.y < 1 or ev.y > content_h:
            return
        row = self.stream._scroll_offset + (ev.y - 1)
        col = max(0, ev.x - 1)
        if ev.button == "left" and ev.press:
            self._selection.clear()
            self._selection.start(row, col)
        elif ev.button == "motion" and self._selection.active:
            self._selection.update(row, col)
        elif ev.button == "left" and ev.release and self._selection.active:
            self._selection.update(row, col)
        self.stream.render()

    def _copy_selection(self) -> None:
        text = extract_selected_text(self.stream, self._selection)
        self._selection.clear()
        if text:
            try:
                set_clipboard(text)
                self.stream.append_block(
                    [Seg(f"  ✓ 已复制 {len(text)} 字符", fg=fg256(42))]
                )
            except Exception as e:
                self.stream.append_block(
                    [Seg(f"  ✖ 复制失败: {e}", fg=fg256(203))]
                )
        else:
            self.stream.render()

    def _queue_send(self) -> None:
        """响应期间按 Enter：把输入入队，当前响应完成后自动发送。"""
        text = self._input_text.strip()
        if not text:
            return
        self._pending_sends.append(text)
        self._input_text = ""
        self._input_cursor = 0
        self.stream.append_block(
            [Seg(f"  ⏎ 已排队 ({len(self._pending_sends)}): {text}", fg=fg256(246))]
        )
        self._render_input()

    def _insert_text(self, ch: str) -> None:
        if len(self._input_text) >= _MAX_INPUT:
            return
        self._input_text = (
            self._input_text[: self._input_cursor]
            + ch
            + self._input_text[self._input_cursor :]
        )
        self._input_cursor += 1
        self._render_input()
        self._sync_command_panel()

    def _backspace(self) -> None:
        if self._input_cursor <= 0:
            return
        self._input_text = (
            self._input_text[: self._input_cursor - 1]
            + self._input_text[self._input_cursor :]
        )
        self._input_cursor -= 1
        self._render_input()
        self._sync_command_panel()

    def _delete(self) -> None:
        if self._input_cursor >= len(self._input_text):
            return
        self._input_text = (
            self._input_text[: self._input_cursor]
            + self._input_text[self._input_cursor + 1 :]
        )
        self._render_input()
        self._sync_command_panel()

    def _history_nav(self, direction: int) -> None:
        if not self._history:
            return
        if self._history_index == -1:
            self._history_draft = self._input_text
            self._history_index = len(self._history) - 1 if direction < 0 else 0
        else:
            self._history_index += direction
            if self._history_index < 0:
                self._history_index = 0
                return
            if self._history_index >= len(self._history):
                self._history_index = -1
                self._input_text = self._history_draft
                self._input_cursor = len(self._input_text)
                self._render_input()
                return
        self._input_text = self._history[self._history_index]
        self._input_cursor = len(self._input_text)
        self._render_input()

    async def _tab_complete(self) -> None:
        text = self._input_text
        if not text:
            return
        if text.startswith("/"):
            matches = complete(self.command_registry, text)
            if matches:
                self._input_text = matches[0][1] + " "
                self._input_cursor = len(self._input_text)
                self._render_input()
        elif "@" in text:
            prefix = text.rsplit("@", 1)[1]
            work_dir = self.agent.work_dir if self.agent else os.getcwd()
            matches = self._scan_files_for_at(prefix, work_dir)
            if matches:
                self._input_text = text.rsplit("@", 1)[0] + "@" + matches[0] + " "
                self._input_cursor = len(self._input_text)
                self._render_input()

    def _scan_files_for_at(self, prefix: str, work_dir: str) -> list[str]:
        _SKIP = {".git", "node_modules", ".venv", "__pycache__", ".meharness", "build", ".gradle"}
        base = os.path.join(work_dir, os.path.dirname(prefix)) if "/" in prefix else work_dir
        name_prefix = os.path.basename(prefix).lower()
        out: list[str] = []
        if not os.path.isdir(base):
            return out
        try:
            for entry in sorted(os.listdir(base)):
                if entry in _SKIP or entry.startswith("."):
                    continue
                if entry.lower().startswith(name_prefix):
                    rel = os.path.join(os.path.dirname(prefix), entry) if "/" in prefix else entry
                    if os.path.isdir(os.path.join(base, entry)):
                        rel += "/"
                    out.append(rel)
                    if len(out) >= 10:
                        break
        except OSError:
            pass
        return out

    async def _cycle_mode(self) -> None:
        if self.agent is None:
            return
        current = self.agent.permission_mode
        try:
            idx = _MODE_CYCLE.index(current)
        except ValueError:
            idx = 0
        next_mode = _MODE_CYCLE[(idx + 1) % len(_MODE_CYCLE)]
        self.agent.set_permission_mode(next_mode)
        self.add_system_message(f"mode → {_MODE_LABELS.get(next_mode, next_mode.value)}")
        self._render_input()

    def _toggle_expand(self) -> None:
        if not self._thinking_accum or self._thinking_block is None:
            return
        if self._thinking_collapsed:
            # 展开：重排为完整思考内容
            self._thinking_collapsed = False
            self.stream.update_block(
                self._thinking_block,
                [Seg("  ∴ ", attrs=DIM + ITALIC)]
                + [Seg(ch, attrs=DIM + ITALIC) for ch in self._thinking_accum],
            )
        else:
            # 折叠：缩成一行提示
            self._thinking_collapsed = True
            self.stream.update_block(
                self._thinking_block,
                [Seg("  ∴ Thinking… (ctrl+o to expand)", attrs=DIM + ITALIC)],
            )

    async def _interrupt(self) -> None:
        if self._streaming and self._agent_task and not self._agent_task.done():
            self._agent_task.cancel()
            self.stream.commit_text("  (response interrupted)")

    async def _submit(self) -> None:
        text = self._input_text.strip()
        if not text:
            return
        self._history.append(text)
        self._history_index = -1
        self._input_text = ""
        self._input_cursor = 0
        self._render_input()
        await self._dispatch(text)

    async def _dispatch(self, text: str) -> None:
        name, args, is_command = parse_command(text)
        if text.strip().lower() in ("exit", "quit") or name.lower() in ("exit", "quit"):
            self._quit = True
            return
        if not is_command:
            if self._streaming or self.agent is None:
                return
            self._agent_task = asyncio.create_task(self._send_message(text))
            return
        if name == "":
            lines = ["可用命令："]
            for cmd in self.command_registry.list_commands():
                aliases_str = ", ".join(f"/{a}" for a in cmd.aliases)
                lines.append(f"  /{cmd.name}, {aliases_str}  {cmd.description}")
            self.add_system_message("\n".join(lines))
            return
        cmd = self.command_registry.find(name)
        if cmd is None:
            self.add_system_message(f"未知命令：/{name}，输入 /help 查看")
            return
        if not args and cmd.arg_prompt:
            self.add_system_message(cmd.arg_prompt)
            return
        ctx = self._build_command_context(args)
        try:
            await cmd.handler(ctx)
        except Exception as e:
            self.add_system_message(f"命令执行失败: {e}")

    def _build_command_context(self, args: str) -> CommandContext:
        def _set_session(s) -> None:
            self.session = s
            if self.agent:
                self.agent.session_id = s.session_id

        def _set_conversation(conv: ConversationManager) -> None:
            self.conversation = conv

        async def _clear_chat() -> None:
            self.stream.commit_text("  --- 已清空（行流式无清屏，保留滚动区） ---")

        async def _render_restored(messages: list[Message]) -> None:
            for msg in messages:
                if msg.tool_results or not msg.content:
                    continue
                if msg.role == "user":
                    self.stream.commit_text(f"❯ {msg.content}")
                elif msg.role == "assistant":
                    self.stream.commit_text(msg.content)

        return CommandContext(
            args=args,
            agent=self.agent,
            conversation=self.conversation,
            session=self.session,
            session_manager=self.session_manager,
            memory_manager=self.memory_manager,
            ui=self,
            config={
                "registry": self.command_registry,
                "set_session": _set_session,
                "set_conversation": _set_conversation,
                "clear_chat": _clear_chat,
                "render_restored": _render_restored,
                "skill_loader": self.skill_loader,
                "skill_executor": self.skill_executor,
            },
        )

    # ------------------------------------------------------------------
    # 消息发送 & 事件渲染
    # ------------------------------------------------------------------

    async def _send_message(self, text: str, is_notification: bool = False) -> None:
        assert self.agent is not None
        self._streaming = True
        if text:
            # 用户消息：灰 ❯ + 整块底色（对齐 claude）
            self.stream.append_block(
                [Seg("❯ ", fg=fg256(246)), Seg(text, bg=bg256(237))]
            )
            self.conversation.add_user_message(text)
            if self.session:
                self.session.append(Message(role="user", content=text))

        # 记忆预取：非阻塞（对齐 claude-code 的 startRelevantMemoryPrefetch——
        # 后台跑，永不阻塞主响应。settle 后在 agent 迭代间注入；本轮没用上就放弃，
        # 下轮会为新消息重新预取）。之前是 await wait_for(..., 3s) 阻塞，每条消息
        # 都要先干等记忆选择再开始响应，体验上"极其缓慢"。
        prefetch_task: asyncio.Task | None = None
        prefetch_consumed = False
        if text:
            prefetch_task = asyncio.create_task(self._prefetch(text))

        self._thinking_start = _time.monotonic()
        self._thinking_verb = "Working"
        self._thinking_accum = ""
        self._thinking_shown = False
        self._thinking_block: str | None = None  # 实时流式显示 thinking 的块
        self._thinking_collapsed = False
        history_cursor = len(self.conversation.history)
        accumulated = ""
        width = self._term_width()
        # 流式锚点块：懒创建——首个 StreamText 到达才建块。**每回合结束
        # （TurnComplete）置回 None，下一回合文本开新块**，否则所有回合的叙述
        # 会堆进同一个 block，表现成"全部累加在一段、工具行全在后面"。
        self._stream_block: str | None = None
        # 当前回合累计原文（TurnComplete 时用 render_markdown 重排成带样式块）
        self._turn_text = ""

        try:
            async for event in self.agent.run(self.conversation):
                if isinstance(event, StreamText):
                    accumulated += event.text
                    self._turn_text += event.text
                    if self._stream_block is None:
                        self._stream_block = self.stream.append_block(
                            [Seg("● ", fg=fg256(173))]
                        )
                    # 增量续写（O(delta)）；之前 update_block 每 token 整段重折行 O(n²)
                    self.stream.update_block_append(
                        self._stream_block, [Seg(event.text)]
                    )
                else:
                    history_cursor = await self._handle_event(
                        event, accumulated, history_cursor
                    )
                # 非阻塞消费记忆预取：settle 了才注入（零等待），未 settle 跳过、
                # 下个迭代或下轮再取。注入后 agent 下一轮构建 api_conv 时会带上。
                if (
                    prefetch_task is not None
                    and not prefetch_consumed
                    and prefetch_task.done()
                ):
                    prefetch_consumed = True
                    try:
                        reminder = prefetch_task.result()
                    except Exception:
                        reminder = ""
                    if reminder:
                        self.conversation.add_system_reminder(reminder)
        except asyncio.CancelledError:
            if accumulated:
                self.stream.commit_text(accumulated + "\n\n*[cancelled]*")
                try:
                    self.conversation.add_assistant_message(
                        accumulated + "\n\n[interrupted by user]"
                    )
                except Exception:
                    pass
        except Exception as e:
            self.stream.commit_text(f"  ✖ {e}")
        finally:
            self._streaming = False
            self._render_input()
            # 预取本轮没用上且还没跑完：取消，避免后台任务泄漏（对齐 claude-code
            # 的 MemoryPrefetch 在生成器退出时 dispose）。
            if (
                prefetch_task is not None
                and not prefetch_consumed
                and not prefetch_task.done()
            ):
                prefetch_task.cancel()
            # 响应期间排队的新消息：当前响应结束后自动发送
            if self._pending_sends:
                nxt = self._pending_sends.pop(0)
                self._agent_task = asyncio.create_task(self._send_message(nxt))

        # 计划模式审批
        if self.agent.plan_mode and self.agent._get_plan_path().exists():
            await self._prompt_plan_approval()

    async def _session_prefetch(self) -> None:
        """会话启动时预热记忆（后台，失败静默），让首条消息就能带上相关记忆。"""
        try:
            work_dir = self.agent.work_dir if self.agent else os.getcwd()
            reminder = await self._prefetch(
                f"Current project: {work_dir}; opening session context."
            )
            if reminder:
                self.conversation.add_system_reminder(reminder)
        except Exception:
            pass

    async def _prefetch(self, query: str) -> str:
        if self.memory_manager is None or self._selected_provider is None:
            return ""
        # 防抖：同一时刻至多一个 side LLM 预取在跑。否则首消息时
        # 会话预取 + 消息预取 + 主响应 并发，且启动预取可能尚未完成。
        if self._prefetch_inflight:
            return ""
        self._prefetch_inflight = True
        provider = self._selected_provider

        async def selector(system_prompt: str, user_message: str) -> str:
            from meharness.tools.base import StreamEnd, TextDelta

            side_client = create_client(provider)
            mini_conv = ConversationManager()
            mini_conv.history = [Message(role="user", content=user_message)]
            collected = ""
            async for event in side_client.stream(mini_conv, system=system_prompt):
                if isinstance(event, TextDelta):
                    collected += event.text
                elif isinstance(event, StreamEnd):
                    pass
            return collected

        try:
            results = await find_relevant_memories(
                query=query,
                user_mem_dir=self.memory_manager.user_mem_dir,
                project_mem_dir=self.memory_manager.project_mem_dir,
                recent_tools=self._recent_tools or None,
                already_surfaced=self._surfaced_memories,
                selector=selector,
            )
            # 记录本次出示的记忆，避免下一轮重复选择（claude already_surfaced）
            for r in results:
                self._surfaced_memories.add(str(r.path))
            return render_reminder(results)
        except Exception:
            return ""
        finally:
            self._prefetch_inflight = False

    def _finalize_stream_block(self) -> None:
        """把当前回合流式累计的纯文本重排成带样式的 markdown 块。

        回合结束（TurnComplete / LoopComplete）时调用一次：标题加粗、代码块
        底色、行内代码变色、粗斜体等，观感对齐 claude-code。已排过（无文本）
        则跳过。
        """
        if self._stream_block is not None and self._turn_text:
            from meharness.markdown import render_markdown

            self.stream.update_block(
                self._stream_block,
                [Seg("● ", fg=fg256(173))] + render_markdown(self._turn_text),
            )

    async def _handle_event(self, event, accumulated: str, history_cursor: int) -> int:
        """处理非流式事件。返回更新后的 history_cursor。"""
        width = self._term_width()

        if isinstance(event, ThinkingText):
            self._thinking_accum += event.text
            self._thinking_shown = True
            if self._thinking_collapsed:
                return
            # 实时流式显示思考内容（暗色斜体）——否则长推理期屏幕静止，
            # 用户感到"卡死"（曾表现为 97s 思考只有一句静态 Thinking…）。
            if self._thinking_block is None:
                self._thinking_block = self.stream.append_block(
                    [Seg("  ∴ ", attrs=DIM + ITALIC)]
                )
            self.stream.update_block_append(
                self._thinking_block, [Seg(event.text, attrs=DIM + ITALIC)]
            )
        elif isinstance(event, RetryEvent):
            self.stream.commit_text(f"  ↻ Retrying: {event.reason}")
        elif isinstance(event, ToolUseEvent):
            verb = tool_verb(event.tool_name, event.arguments)
            block_id = self.stream.append_block(
                [Seg("  ⏳ ", fg=fg256(246)), Seg(f"[{verb}]", fg=fg256(173))]
            )
            # 记录近期工具，供记忆 selector 的 recent_tools 使用（有界，避免无限增长）
            if event.tool_name not in self._recent_tools:
                self._recent_tools.append(event.tool_name)
            if len(self._recent_tools) > 20:
                self._recent_tools = self._recent_tools[-20:]
            # 工具行 spinner（对齐 claude：执行期间动词+转帧）
            self._active_tool_blocks[event.tool_id] = (block_id, verb)
            self._ensure_tool_spinner()
        elif isinstance(event, ToolResultEvent):
            mark = "✗" if event.is_error else "✓"
            color = fg256(203) if event.is_error else fg256(42)
            entry = self._active_tool_blocks.pop(event.tool_id, None)
            verb = entry[1] if entry else event.tool_name
            self.stream.append_block(
                [
                    Seg(f"  {mark} ", fg=color),
                    Seg(f"[{verb}]", fg=color),
                    Seg(f" ({event.elapsed:.1f}s)", fg=fg256(246)),
                ]
            )
            if not self._active_tool_blocks and self._tool_spinner_timer is not None:
                self._tool_spinner_timer.cancel()
                self._tool_spinner_timer = None
        elif isinstance(event, TurnComplete):
            # 回合结束：把该回合的流式纯文本重排成带样式的 markdown 块
            # （标题加粗/代码块底色/行内代码变色，对齐 claude 观感），再终结。
            self._finalize_stream_block()
            # 下一回合文本开新块（对齐 claude 每步一段）
            self._stream_block = None
            if self.session:
                for msg in self.conversation.history[history_cursor:]:
                    self.session.append(msg)
                history_cursor = len(self.conversation.history)
        elif isinstance(event, LoopComplete):
            # 最终回合（无工具调用路径）不 yield TurnComplete，这里也要重排
            self._finalize_stream_block()
            self._turn_text = ""
            self._stream_block = None
            total_time = _time.monotonic() - self._thinking_start
            if self.agent:
                done = (
                    f"✻ Done for {total_time:.1f}s"
                    f"  (in {_fmt_k(self.agent.total_input_tokens)}"
                    f" · out {_fmt_k(self.agent.total_output_tokens)})"
                )
            else:
                done = f"✻ Done for {total_time:.1f}s"
            self.stream.commit_text(DIM + done + RESET)
            if self.session:
                for msg in self.conversation.history[history_cursor:]:
                    self.session.append(msg)
                self.session.meta.total_tokens = (
                    self.agent.total_input_tokens + self.agent.total_output_tokens
                )
                history_cursor = len(self.conversation.history)
        elif isinstance(event, CompactNotification):
            self.stream.commit_text(f"  ↻ {event.message}")
            self._push_banner(event.message, FG_YELLOW)
        elif isinstance(event, ErrorEvent):
            self.stream.commit_text(f"  ✖ {event.message}")
            self._push_banner(event.message, FG_RED)
        elif isinstance(event, HookEvent):
            status = "✓" if event.success else "✗"
            self.stream.commit_text(f"  Hook [{event.hook_id}] {status} {event.output}")
        elif isinstance(event, PermissionRequest):
            await self._prompt_permission(event)
        elif isinstance(event, AskUserEvent):
            await self._prompt_askuser(event)
        return history_cursor

    # ------------------------------------------------------------------
    # Overlay 基础设施（对齐 claude 交互层）
    # ------------------------------------------------------------------

    def _open_overlay(self, ov) -> None:
        """挂载 overlay 并绑定 result future。"""
        ov.result = asyncio.get_running_loop().create_future()
        self.stream.push_overlay(ov)

    def _close_overlay(self, ov) -> None:
        """仅当 ov 仍在栈顶时弹栈（避免误关新 overlay / 重复弹栈）。"""
        if self.stream.top_overlay() is ov:
            self.stream.pop_overlay()

    async def _run_overlay(self, ov: Overlay) -> Any:
        """挂载 overlay 并等待用户选择，返回 overlay 的 result。

        _send_message 是后台任务，主循环（run() 里 _keys 的消费方）一直在跑；
        按键统一由主循环 _handle_key 路由到栈顶 overlay。这里**不能**自己去
        ``await self._keys.get()`` 读键 —— 会和主循环 FIFO 竞争，主循环总是
        先拿到键，overlay 永远等不到输入，事件 future 永不 resolve，agent 干等
        超时（曾表现为"卡在 AskUserEvent / 权限面板 / Bash 命令"）。
        所以只 await ov.result：主循环喂键 → on_key resolve → 主循环 pop。
        """
        self._open_overlay(ov)
        try:
            return await ov.result
        finally:
            self._close_overlay(ov)

    def _sync_command_panel(self) -> None:
        """输入以 / 开头时打开/刷新斜杠补全面板；否则关闭。"""
        text = self._input_text
        ov = self.stream.top_overlay()
        if text.startswith("/") and not self._streaming:
            cmds = match_commands(self.command_registry, text)
            if cmds:
                if isinstance(ov, CommandCompletionOverlay):
                    ov.commands = cmds
                    ov.options = [f"/{c.name}  {c.description}" for c in cmds]
                    if ov.focus >= len(cmds):
                        ov.focus = 0
                else:
                    new_ov = CommandCompletionOverlay(cmds)
                    new_ov.result = asyncio.get_running_loop().create_future()
                    self.stream.push_overlay(new_ov)
                self.stream.render()
                return
        if isinstance(ov, CommandCompletionOverlay):
            self.stream.pop_overlay()

    async def _run_command_panel(self, ov: CommandCompletionOverlay, index: int | None) -> None:
        """补全面板 Enter 确认后执行选中命令（index 可为 None=无选择）。"""
        if index is None or index >= len(ov.commands):
            return
        cmd = ov.commands[index]
        text = self._input_text.lstrip("/")
        # 保留参数部分：/name arg1 arg2
        rest = text.split(" ", 1)[1] if " " in text else ""
        self._input_text = ""
        self._input_cursor = 0
        self._render_input()
        await self._dispatch(f"/{cmd.name}" + (f" {rest}" if rest else ""))

    def _ensure_tool_spinner(self) -> None:
        """工具执行时启动单例转帧定时器；全部结束后停止。"""
        if self._tool_spinner_timer is not None and not self._tool_spinner_timer.done():
            return
        frames = _SPINNER_FRAMES

        async def _spin() -> None:
            while self._active_tool_blocks:
                frame = frames[self._tool_spinner_frame % len(frames)]
                self._tool_spinner_frame += 1
                for _tool_id, (block_id, verb) in list(self._active_tool_blocks.items()):
                    self.stream.update_block(
                        block_id,
                        [
                            Seg(f"  {frame} ", fg=fg256(246)),
                            Seg(f"[{verb}]", fg=fg256(173)),
                        ],
                    )
                await asyncio.sleep(0.1)

        self._tool_spinner_timer = asyncio.create_task(_spin())

    def _push_banner(self, text: str, color: str = "") -> None:
        ov = BannerOverlay(text, color)
        self.stream.push_overlay(ov)

        async def _auto_close() -> None:
            await asyncio.sleep(4)
            self._close_overlay(ov)

        asyncio.create_task(_auto_close())

    def _term_width(self) -> int:
        try:
            return os.get_terminal_size().columns
        except OSError:
            return 80

    # ------------------------------------------------------------------
    # 权限 / AskUser / 计划审批（保留 future 契约）
    # ------------------------------------------------------------------

    async def _prompt_permission(self, event: PermissionRequest) -> None:
        # 对齐 claude 权限面板：展示工具 + 参数，内联允许/始终/拒绝/编辑
        ov = PermissionOverlay(event.tool_name, event.description, event.arguments)
        choice = await self._run_overlay(ov)
        if choice == "allow":
            resp = PermissionResponse.ALLOW
        elif choice == "always":
            resp = PermissionResponse.ALLOW_ALWAYS
        elif choice == "edit":
            # 编辑参数：输入替换的 JSON，原地修改 event.arguments 后按允许执行
            edited = await self._collect_other_input()
            if edited.strip():
                try:
                    import json as _json

                    new_args = _json.loads(edited)
                    if isinstance(new_args, dict):
                        event.arguments = new_args
                except Exception:
                    pass
            resp = PermissionResponse.ALLOW
        else:
            resp = PermissionResponse.DENY
        if not event.future.done():
            event.future.set_result(resp)
        self._render_input()

    async def _prompt_askuser(self, event: AskUserEvent) -> None:
        questions = event.questions
        answers: dict[str, str] = {}
        for q in questions:
            header = q.get("message", q.get("question", "Question"))
            name = q.get("name", header)  # 答案键必须用 name，工具按 q.name 读取
            options = q.get("options", [])
            multi = q.get("type") == "checkbox" or q.get("multiSelect", False)
            labels = [o.get("label", str(o)) if isinstance(o, dict) else str(o) for o in options]
            # 焦点化选择面板（对齐 claude AskUserQuestion）
            ov = AskUserOverlay(header, labels, bool(multi))
            choice = await self._run_overlay(ov)
            if choice is None:
                answers[name] = ""
            elif choice == "other":
                answers[name] = await self._collect_other_input()
            else:
                if multi:
                    answers[name] = ", ".join(labels[i] for i in choice)
                else:
                    answers[name] = labels[choice[0]]
        if not event.future.done():
            event.future.set_result(answers if answers else {})
        self._render_input()

    async def _collect_other_input(self) -> str:
        # 用 TextInputOverlay 捕获自由文本：按键由主循环驱动（与 _run_overlay
        # 同因，不能自己读 _keys）。Esc 返回空串，与旧行为一致。
        ov = TextInputOverlay()
        return await self._run_overlay(ov) or ""

    async def _prompt_plan_approval(self) -> None:
        plan_path = self.agent._get_plan_path()
        plan_content = ""
        if plan_path.exists():
            try:
                plan_content = plan_path.read_text(encoding="utf-8")
            except Exception:
                pass
        # 对齐 claude plan 对话框：计划全文 + 焦点选择
        ov = PlanOverlay(plan_content)
        choice = await self._run_overlay(ov)
        choice = choice if choice is not None else 2
        pre = getattr(self, "_pre_plan_mode", PermissionMode.DEFAULT)
        if choice == 0:
            self.agent.set_permission_mode(PermissionMode.BYPASS)
            if plan_content:
                self.stream.commit_text("  ↻ 执行计划（YOLO）")
                self.send_user_message(f"Execute this plan:\n\n{plan_content}")
        elif choice == 1:
            self.agent.set_permission_mode(pre)
            if plan_content:
                self.stream.commit_text("  ↻ 执行计划（手动批准）")
                self.send_user_message(f"Execute this plan:\n\n{plan_content}")
        else:
            self.agent.set_permission_mode(pre)
            feedback = await self._collect_other_input()
            if feedback:
                self.send_user_message(feedback)
        self._render_input()
