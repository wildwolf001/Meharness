"""TUI overlay 交互层（观感对齐 Claude Code）。

Overlay 渲染由 FullscreenScreen 覆写内容区完成；本模块提供具体 overlay 与
纯函数（供单测）。按键路由：ReplApp._handle_key 优先把按键交给栈顶 overlay，
overlay 通过 result future 把选择交还等待方。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from meharness.term.ansi import DIM, RESET, REVERSE, fg256, styled
from meharness.term.screen import Overlay, cell_width

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _pw(s: str) -> int:
    """忽略 ANSI 转义后的显示宽度（CJK 2 格）。"""
    return sum(cell_width(ch) for ch in _ANSI_RE.sub("", s))


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _pw(s))


def _truncate(s: str, width: int) -> str:
    w = 0
    out = ""
    for ch in s:
        cw = cell_width(ch)
        if w + cw > width:
            break
        out += ch
        w += cw
    return out


# ---------------------------------------------------------------------------
# 盒子
# ---------------------------------------------------------------------------

def box(title: str, content: list[str], width: int, footer: str = "") -> list[str]:
    """画一个带标题/可选中 footer 的边框盒。宽度为盒子总宽度。"""
    inner = max(2, width - 4)
    lines: list[str] = []
    t = _truncate(title, inner - 2)
    lines.append("┌─ " + t + " " + "─" * max(0, width - 5 - _pw(t)) + "┐")
    for c in content:
        lines.append("│ " + _pad(c, inner) + " │")
    if footer:
        f = _truncate(footer, inner)
        lines.append("│ " + _pad(f, inner) + " │")
    lines.append("└" + "─" * (inner + 2) + "┘")
    return lines


# ---------------------------------------------------------------------------
# 通用可聚焦列表（斜杠命令 / plan 选择复用）
# ---------------------------------------------------------------------------

class ListOverlay(Overlay):
    """通用焦点列表：up/down 移焦，enter 确认，escape 取消。"""

    MAX_VISIBLE = 8

    def __init__(self, title: str, options: list[str], footer: str = "") -> None:
        self.title = title
        self.options = list(options)
        self.footer = footer
        self.focus = 0
        self.result: asyncio.Future[int | None] | None = None

    def lines(self, width: int) -> list[str]:
        start = max(0, min(self.focus - 3, len(self.options) - self.MAX_VISIBLE))
        content: list[str] = []
        for i in range(start, min(len(self.options), start + self.MAX_VISIBLE)):
            opt = self.options[i]
            if i == self.focus:
                content.append(REVERSE + " ❯ " + _truncate(opt, width - 8) + RESET)
            else:
                content.append("   " + _truncate(opt, width - 8))
        if not content:
            content.append("   (空)")
        return box(self.title, content, width, self.footer)

    async def on_key(self, key) -> bool:
        kind = key[0]
        n = len(self.options)
        if kind == "up" and n:
            self.focus = (self.focus - 1) % n
            return True
        if kind == "down" and n:
            self.focus = (self.focus + 1) % n
            return True
        if kind == "enter":
            if self.result is not None and not self.result.done():
                self.result.set_result(self.focus if n else None)
            return True
        if kind == "escape":
            if self.result is not None and not self.result.done():
                self.result.set_result(None)
            return True
        return False


# ---------------------------------------------------------------------------
# 斜杠命令补全面板
# ---------------------------------------------------------------------------

def match_commands(registry: Any, prefix: str) -> list[Any]:
    """返回 name 以 prefix（不含前导 /）开头的命令，保持注册顺序。"""
    p = prefix.lstrip("/").lower()
    if not p:
        return []
    return [c for c in registry.list_commands() if c.name.startswith(p) or p in c.name]


class CommandCompletionOverlay(ListOverlay):
    """斜杠命令补全面板。

    非 modal：print/backspace 落回输入编辑继续过滤（ReplApp 在每次输入变化后
    调 _sync_command_panel 刷新候选）。Enter 把选中的命令索引放入 result。
    """

    modal = False

    def __init__(self, commands: list[Any]) -> None:
        super().__init__("Commands", [], "[↑/↓] 选择   [Enter] 执行   [Esc] 关闭")
        self.commands = list(commands)
        self.options = [f"/{c.name}  {c.description}" for c in self.commands]
        self.focus = 0
        self.result = None


# ---------------------------------------------------------------------------
# 权限内联面板
# ---------------------------------------------------------------------------

class PermissionOverlay(Overlay):
    """claude 风格权限面板：标题 + 参数 + footer 动作（Enter allow / a always / d deny / e edit）。"""

    def __init__(self, tool_name: str, description: str, params: dict[str, Any] | None) -> None:
        self.tool_name = tool_name
        self.description = description
        self.params = params or {}
        self.result: asyncio.Future[str] | None = None  # "allow"|"always"|"deny"|"edit"

    def lines(self, width: int) -> list[str]:
        content: list[str] = []
        content.append(styled(f"Tool: {self.tool_name}", DIM))
        if self.description:
            content.append(_truncate(self.description, width - 6))
        content.append("")
        content.append("Parameters:")
        if self.params:
            for k, v in list(self.params.items())[:6]:
                content.append("  " + _truncate(f"{k}: {v}", width - 8))
            if len(self.params) > 6:
                content.append(f"  …（共 {len(self.params)} 项）")
        else:
            content.append("  (none)")
        return box(
            f"Permission: {self.tool_name}",
            content,
            width,
            "[Enter] Allow   [a] Always   [d] Deny   [e] Edit   [Esc] Deny",
        )

    async def on_key(self, key) -> bool:
        kind = key[0]
        payload = key[1] if len(key) > 1 else ""
        if kind == "enter":
            self._resolve("allow")
            return True
        if kind == "print":
            pl = payload.lower()
            if pl == "a":
                self._resolve("always")
                return True
            if pl == "d":
                self._resolve("deny")
                return True
            if pl == "e":
                self._resolve("edit")
                return True
        if kind == "escape":
            self._resolve("deny")
            return True
        return False

    def _resolve(self, choice: str) -> None:
        if self.result is not None and not self.result.done():
            self.result.set_result(choice)


# ---------------------------------------------------------------------------
# Plan Mode 对话框
# ---------------------------------------------------------------------------

class PlanOverlay(Overlay):
    CHOICES = [
        "执行计划（YOLO 自动批准）",
        "执行计划（手动批准）",
        "告诉我改什么",
    ]

    def __init__(self, plan_content: str) -> None:
        self.plan = plan_content
        self.focus = 0
        self.result: asyncio.Future[int | None] | None = None

    def lines(self, width: int) -> list[str]:
        plan_lines = [l for l in self.plan.splitlines() if l.strip()][:6] or ["(空计划)"]
        content: list[str] = []
        for i, l in enumerate(plan_lines):
            content.append(_truncate("  " + l, width - 6))
            if i >= 5:
                content.append("  …（计划较长，已折叠）")
                break
        content.append("")
        content.append("如何执行？")
        for i, c in enumerate(self.CHOICES):
            mark = "❯" if i == self.focus else " "
            content.append((REVERSE + f" {mark} {c}" + RESET) if i == self.focus else f" {mark} {c}")
        return box("Plan Mode", content, width, "[↑/↓] 选择   [Enter] 确认   [Esc] 取消")

    async def on_key(self, key) -> bool:
        kind = key[0]
        if kind == "up":
            self.focus = (self.focus - 1) % len(self.CHOICES)
            return True
        if kind == "down":
            self.focus = (self.focus + 1) % len(self.CHOICES)
            return True
        if kind == "enter":
            if self.result is not None and not self.result.done():
                self.result.set_result(self.focus)
            return True
        if kind == "escape":
            if self.result is not None and not self.result.done():
                self.result.set_result(2)
            return True
        return False


# ---------------------------------------------------------------------------
# AskUserQuestion 焦点化
# ---------------------------------------------------------------------------

class AskUserOverlay(Overlay):
    """单道 AskUser 问题：焦点选择，空格多选，末项为 Other 自定义。"""

    def __init__(self, question: str, options: list[str], multi: bool) -> None:
        self.question = question
        self.options = list(options)
        self.multi = multi
        self.focus = 0
        self.selected: set[int] = set()
        self.result: asyncio.Future | None = None  # (indices,) | "other" | None

    def _n(self) -> int:
        return len(self.options) + 1  # +1 = Other

    def lines(self, width: int) -> list[str]:
        content: list[str] = [_truncate(self.question, width - 6), ""]
        for i, opt in enumerate(self.options):
            mark = "❯" if i == self.focus else " "
            check = "☑" if i in self.selected else "☐"
            line = f"{mark} {check} {_truncate(opt, width - 12)}"
            content.append(REVERSE + " " + line + RESET if i == self.focus else line)
        other_idx = len(self.options)
        omark = "❯" if other_idx == self.focus else " "
        content.append(
            (REVERSE + f" {omark} Other（自定义输入）" + RESET)
            if other_idx == self.focus
            else f" {omark} Other（自定义输入）"
        )
        footer = (
            "[↑/↓] 选择   [Space] 多选   [Enter] 确认"
            if self.multi
            else "[↑/↓] 选择   [Enter] 确认"
        )
        return box("AskUserQuestion", content, width, footer)

    async def on_key(self, key) -> bool:
        kind = key[0]
        payload = key[1] if len(key) > 1 else ""
        n = self._n()
        if kind == "up":
            self.focus = (self.focus - 1) % n
            return True
        if kind == "down":
            self.focus = (self.focus + 1) % n
            return True
        if kind == "print" and payload == " " and self.multi and self.focus < len(self.options):
            i = self.focus
            self.selected.discard(i) if i in self.selected else self.selected.add(i)
            return True
        if kind == "print" and payload == "0":
            self._resolve("other")
            return True
        if kind == "enter":
            if self.focus == len(self.options):
                self._resolve("other")
            elif self.multi:
                self._resolve(tuple(sorted(self.selected)))
            else:
                self._resolve((self.focus,))
            return True
        if kind == "escape":
            self._resolve(None)
            return True
        return False

    def _resolve(self, value: Any) -> None:
        if self.result is not None and not self.result.done():
            self.result.set_result(value)


# ---------------------------------------------------------------------------
# 自由文本输入（AskUser Other / 权限编辑参数 / 计划反馈）
# ---------------------------------------------------------------------------

class TextInputOverlay(Overlay):
    """捕获一行自由文本。由 ReplApp 主循环驱动按键（不能自己读 _keys，
    否则和主循环 FIFO 竞争死锁）；Enter 提交、Esc 返回空串。"""

    def __init__(self, prompt: str = "  ➤ ") -> None:
        self.prompt = prompt
        self.text = ""
        self.result: asyncio.Future[str] | None = None

    def lines(self, width: int) -> list[str]:
        content = [self.prompt + self.text, ""]
        return box("Input", content, width, "[Enter] 确认   [Esc] 取消")

    async def on_key(self, key) -> bool:
        kind = key[0]
        if kind == "print":
            self.text += key[1]
            return True
        if kind == "backspace":
            self.text = self.text[:-1]
            return True
        if kind == "enter":
            self._resolve(self.text)
            return True
        if kind == "escape":
            self._resolve("")
            return True
        return False

    def _resolve(self, value: str) -> None:
        if self.result is not None and not self.result.done():
            self.result.set_result(value)


# ---------------------------------------------------------------------------
# 横幅（RateLimit / Token 告警 / 错误）
# ---------------------------------------------------------------------------

class BannerOverlay(Overlay):
    """锚顶单行横幅，TTL 由 ReplApp 管理。非 modal：不吞键，只是视觉提示。"""

    modal = False

    def __init__(self, text: str, color: str = "") -> None:
        self.text = text
        self.color = color

    def centered(self) -> bool:
        return False

    def lines(self, width: int) -> list[str]:
        return [_pad(self.color + "⚠ " + self.text + RESET, width)]


# ---------------------------------------------------------------------------
# 工具执行动词（对齐 claude getActivityDescription）
# ---------------------------------------------------------------------------

def tool_verb(name: str, args: dict[str, Any] | None = None) -> str:
    """根据工具名与参数生成"正在做什么"的短动词串，供工具行 spinner 显示。"""
    args = args or {}
    target = ""
    for k in ("file_path", "path", "pattern", "query", "url", "command"):
        if k in args and args[k]:
            target = str(args[k])
            break
    verbs = {
        "ReadFile": "Reading",
        "WriteFile": "Writing",
        "EditFile": "Editing",
        "Bash": "Running command",
        "Glob": "Searching",
        "Grep": "Searching",
        "WebSearch": "Searching",
        "WebFetch": "Fetching",
        "LoadSkill": "Loading skill",
        "Agent": "Delegating to subagent",
        "TaskCreate": "Creating task",
        "TaskList": "Listing tasks",
        "TaskGet": "Reading task",
        "TaskUpdate": "Updating task",
        "EnterWorktree": "Entering worktree",
        "ExitWorktree": "Leaving worktree",
    }
    verb = verbs.get(name, "Running tool")
    if target:
        return f"{verb} {_truncate(target, 40)}"
    return verb
