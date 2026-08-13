# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Static

from meharness.agent import PermissionResponse


_PERM_OPTIONS = [
    ("Yes", PermissionResponse.ALLOW),
    ("Yes, and don't ask again for this pattern", PermissionResponse.ALLOW_ALWAYS),
    ("No", PermissionResponse.DENY),
]


class InlinePermissionWidget(Vertical, can_focus=True):
    """渲染在聊天区域内部的内联权限确认提示。

    与 Go 版 TUI 的权限对话框一致：工具名 + 描述 + 带编号的
    选项，支持方向键导航 + 回车确认。
    """

    BINDINGS = [
        Binding("up", "cursor_up", "Up", priority=True),
        Binding("down", "cursor_down", "Down", priority=True),
        Binding("enter", "select", "Select", priority=True),
        Binding("escape", "deny", "Deny", priority=True),
        # 数字键快捷选择：即使方向键在部分终端（mintty）失灵也能操作
        Binding("1", "option_1", "Yes", priority=True),
        Binding("2", "option_2", "Yes Always", priority=True),
        Binding("3", "option_3", "No", priority=True),
    ]

    class Responded(Message):


        def __init__(self, response: PermissionResponse) -> None:
            super().__init__()
            self.response = response

    def __init__(self, tool_name: str, description: str, **kwargs) -> None:
        super().__init__(id="perm-inline", **kwargs)
        self._tool_name = tool_name
        self._description = description
        self._cursor = 0

    def compose(self) -> ComposeResult:
        yield Static(self._build_content(), id="perm-content")


    def on_mount(self) -> None:
        self.focus()

    def _build_content(self) -> str:
        lines = []
        lines.append(f"\n  [bold yellow]{self._tool_name} command[/bold yellow]\n")
        lines.append(f"    {self._description}\n")
        lines.append("  [dim]This command requires approval[/dim]\n")
        lines.append("  Do you want to proceed?\n")

        for i, (label, _resp) in enumerate(_PERM_OPTIONS):
            if i == self._cursor:
                lines.append(f" [bold cyan]❯[/bold cyan] {i + 1}. [bold]{label}[/bold]")
            else:
                lines.append(f"   {i + 1}. [dim]{label}[/dim]")

        return "\n".join(lines)


    def _refresh(self) -> None:
        content = self.query_one("#perm-content", Static)
        content.update(self._build_content())

    def action_cursor_up(self) -> None:
        if self._cursor > 0:
            self._cursor -= 1
            self._refresh()

    def action_cursor_down(self) -> None:
        if self._cursor < len(_PERM_OPTIONS) - 1:
            self._cursor += 1
            self._refresh()

    def action_select(self) -> None:
        _, response = _PERM_OPTIONS[self._cursor]
        self.post_message(self.Responded(response))


    def action_deny(self) -> None:
        self.post_message(self.Responded(PermissionResponse.DENY))

    def action_option_1(self) -> None:
        self._cursor = 0
        self.action_select()

    def action_option_2(self) -> None:
        self._cursor = 1
        self.action_select()

    def action_option_3(self) -> None:
        self._cursor = 2
        self.action_select()
