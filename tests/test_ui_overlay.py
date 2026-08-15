# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
"""TUI overlay 层纯函数与交互测试（不依赖真实终端）。"""

from __future__ import annotations

import asyncio
import re

import pytest

from meharness.term.overlays import (
    AskUserOverlay,
    BannerOverlay,
    ListOverlay,
    PermissionOverlay,
    PlanOverlay,
    TextInputOverlay,
    box,
    match_commands,
    tool_verb,
)
from meharness.term.screen import FullscreenScreen, Seg, display_width


class TestToolVerb:
    def test_read(self) -> None:
        assert tool_verb("ReadFile", {"file_path": "a.py"}) == "Reading a.py"

    def test_bash(self) -> None:
        assert tool_verb("Bash", {"command": "ls -la"}) == "Running command ls -la"

    def test_search(self) -> None:
        assert tool_verb("Grep", {"pattern": "foo"}) == "Searching foo"

    def test_agent(self) -> None:
        assert tool_verb("Agent", {}) == "Delegating to subagent"

    def test_fallback(self) -> None:
        assert tool_verb("UnknownTool", {}) == "Running tool"


class TestMatchCommands:
    def _registry(self):
        from meharness.commands import CommandRegistry
        from meharness.commands.handlers import register_all_commands

        r = CommandRegistry()
        register_all_commands(r)
        return r

    def test_prefix_match(self) -> None:
        names = {c.name for c in match_commands(self._registry(), "/co")}
        assert {"compact", "copy", "cost"} <= names

    def test_empty_prefix_no_match(self) -> None:
        assert match_commands(self._registry(), "/") == []


class TestBox:
    def test_rows_have_consistent_width(self) -> None:
        for width in (20, 30, 60):
            rows = box("Title", ["hello world"], width, "footer")
            assert all(display_width(r) == width for r in rows), (
                width,
                [display_width(r) for r in rows],
            )

    def test_cjk_handling(self) -> None:
        rows = box("中文标题", ["内容内容内容"], 24, "")
        assert all(display_width(r) == 24 for r in rows)


@pytest.mark.asyncio
class TestListOverlay:
    async def test_up_down_enter(self) -> None:
        ov = ListOverlay("M", ["a", "b", "c"])
        ov.result = asyncio.get_running_loop().create_future()
        assert await ov.on_key(("down",)) is True
        assert ov.focus == 1
        assert await ov.on_key(("down",)) is True
        assert ov.focus == 2
        assert await ov.on_key(("up",)) is True
        assert ov.focus == 1
        assert await ov.on_key(("enter",)) is True
        assert ov.result.done() and ov.result.result() == 1

    async def test_escape_returns_none(self) -> None:
        ov = ListOverlay("M", ["a", "b"])
        ov.result = asyncio.get_running_loop().create_future()
        await ov.on_key(("escape",))
        assert ov.result.done() and ov.result.result() is None


@pytest.mark.asyncio
class TestPermissionOverlay:
    def _make(self):
        return PermissionOverlay("Bash", "desc", {"command": "ls"})

    async def test_allow(self) -> None:
        ov = self._make()
        ov.result = asyncio.get_running_loop().create_future()
        await ov.on_key(("enter",))
        assert ov.result.result() == "allow"

    async def test_deny_via_d(self) -> None:
        ov = self._make()
        ov.result = asyncio.get_running_loop().create_future()
        await ov.on_key(("print", "d"))
        assert ov.result.result() == "deny"

    async def test_edit(self) -> None:
        ov = self._make()
        ov.result = asyncio.get_running_loop().create_future()
        await ov.on_key(("print", "e"))
        assert ov.result.result() == "edit"


@pytest.mark.asyncio
class TestPlanOverlay:
    async def test_enter_yolo(self) -> None:
        ov = PlanOverlay("plan content")
        ov.result = asyncio.get_running_loop().create_future()
        await ov.on_key(("down",))
        await ov.on_key(("down",))
        await ov.on_key(("up",))
        await ov.on_key(("up",))
        await ov.on_key(("enter",))
        assert ov.result.result() == 0

    async def test_escape_defaults_to_manual(self) -> None:
        ov = PlanOverlay("plan")
        ov.result = asyncio.get_running_loop().create_future()
        await ov.on_key(("escape",))
        assert ov.result.result() == 2


@pytest.mark.asyncio
class TestAskUserOverlay:
    async def test_single_select(self) -> None:
        ov = AskUserOverlay("q", ["a", "b"], multi=False)
        ov.result = asyncio.get_running_loop().create_future()
        await ov.on_key(("down",))
        await ov.on_key(("enter",))
        assert ov.result.result() == (1,)

    async def test_multi_select_space(self) -> None:
        ov = AskUserOverlay("q", ["a", "b", "c"], multi=True)
        ov.result = asyncio.get_running_loop().create_future()
        await ov.on_key(("print", " "))
        await ov.on_key(("down",))
        await ov.on_key(("print", " "))
        await ov.on_key(("enter",))
        assert ov.result.result() == (0, 1)

    async def test_other(self) -> None:
        ov = AskUserOverlay("q", ["a"], multi=False)
        ov.result = asyncio.get_running_loop().create_future()
        await ov.on_key(("down",))  # focus 移到 Other
        await ov.on_key(("enter",))
        assert ov.result.result() == "other"


@pytest.mark.asyncio
class TestTextInputOverlay:
    async def test_type_and_enter(self) -> None:
        ov = TextInputOverlay()
        ov.result = asyncio.get_running_loop().create_future()
        for ch in "hello":
            await ov.on_key(("print", ch))
        assert ov.text == "hello"
        await ov.on_key(("enter",))
        assert ov.result.result() == "hello"

    async def test_backspace(self) -> None:
        ov = TextInputOverlay()
        ov.result = asyncio.get_running_loop().create_future()
        await ov.on_key(("print", "a"))
        await ov.on_key(("print", "b"))
        await ov.on_key(("backspace",))
        await ov.on_key(("enter",))
        assert ov.result.result() == "a"

    async def test_escape_returns_empty(self) -> None:
        ov = TextInputOverlay()
        ov.result = asyncio.get_running_loop().create_future()
        await ov.on_key(("print", "x"))
        await ov.on_key(("escape",))
        assert ov.result.result() == ""


class TestTextInputOverlayStatic:
    def test_modal_by_default(self) -> None:
        # 文本输入是 modal：主循环把所有键路由给它，不落回输入框
        assert getattr(TextInputOverlay(), "modal", True) is True


class TestRunOverlayCooperation:
    """回归：_run_overlay 不能自己读键。

    旧实现 `await self._keys.get()` 与主循环 FIFO 竞争——主循环先注册总是先
    拿到键，overlay 永远等不到输入，事件 future 永不 resolve（曾表现为"卡在
    AskUserEvent / 权限面板 / Bash 命令"）。修复后 overlay 由主循环路由按键、
    on_key resolve result，_run_overlay 只 await ov.result。下面用真实
    FullscreenScreen + 模拟主循环验证这条链路不再死锁。
    """

    class _FakeOut:
        def __init__(self) -> None:
            self.data = ""

        def write(self, s: str) -> None:
            self.data += s

        def flush(self) -> None:
            pass

    def _screen(self) -> FullscreenScreen:
        s = FullscreenScreen(out=self._FakeOut())
        s._cols = 40
        s._rows = 12
        s.enter()
        return s

    @pytest.mark.asyncio
    async def test_overlay_resolves_via_main_loop(self) -> None:
        screen = self._screen()
        keys: asyncio.Queue = asyncio.Queue()
        ov = AskUserOverlay("q", ["a", "b"], multi=False)

        # 模拟 ReplApp 主循环 _handle_key 的路由：只从 keys 读键，从栈顶向下
        # 走，第一个消费的 overlay 赢，resolve 后 remove_overlay。它是唯一消费者。
        async def main_loop() -> None:
            while True:
                key = await keys.get()
                o = screen.top_overlay()
                while o is not None:
                    consumed = await o.on_key(key)
                    if ov.result is not None and ov.result.done():
                        screen.remove_overlay(o)
                        return
                    if consumed:
                        break
                    if getattr(o, "modal", True):
                        break
                    o = screen.overlay_below(o)

        # 模拟修复后的 _run_overlay：只 await result，绝不自读 keys。
        async def run_overlay():
            ov.result = asyncio.get_running_loop().create_future()
            screen.push_overlay(ov)
            return await ov.result

        t_main = asyncio.create_task(main_loop())
        await asyncio.sleep(0)  # 主循环先注册 get（旧代码死锁的关键：先注册者赢）
        t_ov = asyncio.create_task(run_overlay())
        await asyncio.sleep(0)

        keys.put_nowait(("enter",))
        ans, _ = await asyncio.wait_for(
            asyncio.gather(t_ov, t_main), timeout=1.0
        )
        assert ans == (0,)
        assert screen.top_overlay() is None

    @pytest.mark.asyncio
    async def test_banner_above_interactive_overlay_does_not_block_keys(self) -> None:
        # 回归：BannerOverlay 非 modal，压在 AskUserOverlay 之上时按键必须
        # 穿过它到达下层，否则 AskUser 在 token 告警期间会再次"卡住"。
        screen = self._screen()
        banner = BannerOverlay("token warning")
        ask = AskUserOverlay("q", ["a"], multi=False)
        ask.result = asyncio.get_running_loop().create_future()
        screen.push_overlay(ask)
        screen.push_overlay(banner)

        async def route(key) -> bool:
            o = screen.top_overlay()
            while o is not None:
                consumed = await o.on_key(key)
                if getattr(o, "result", None) is not None and o.result.done():
                    screen.remove_overlay(o)
                    return True
                if consumed:
                    return True
                if getattr(o, "modal", True):
                    return False
                o = screen.overlay_below(o)
            return False

        # banner 不消费 Enter → 落到 AskUserOverlay → resolve
        assert await route(("enter",)) is True
        assert ask.result.done()
        assert screen.top_overlay() is banner  # banner 仍在，只是 AskUser 已关


class TestBannerOverlay:
    def test_anchored_top(self) -> None:
        ov = BannerOverlay("hello", "")
        assert ov.centered() is False
        assert ov.anchor_top(20, 80) == 1

    def test_lines(self) -> None:
        ov = BannerOverlay("hello")
        rows = ov.lines(20)
        assert len(rows) == 1
        plain = re.sub(r"\x1b\[[0-9;]*m", "", rows[0])
        assert display_width(plain) == 20

    def test_non_modal_does_not_swallow_keys(self) -> None:
        # 横幅只是视觉提示：modal=False，显示期间不吞掉用户按键
        assert BannerOverlay("hello").modal is False


class TestScreenOverlay:
    class _FakeOut:
        def __init__(self) -> None:
            self.data = ""

        def write(self, s: str) -> None:
            self.data += s

        def flush(self) -> None:
            pass

    def _screen(self) -> FullscreenScreen:
        s = FullscreenScreen(out=self._FakeOut())
        s._cols = 40
        s._rows = 12
        s.enter()
        return s

    def _plain(self, s: FullscreenScreen) -> str:
        return re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(s._screen_rows))

    def test_push_pop_top(self) -> None:
        s = self._screen()
        ov = BannerOverlay("hi")
        assert s.top_overlay() is None
        s.push_overlay(ov)
        assert s.top_overlay() is ov
        assert s.pop_overlay() is ov
        assert s.top_overlay() is None

    def test_render_applies_overlay_rows(self) -> None:
        s = self._screen()
        s.append_block([Seg("content line", fg="")])
        ov = BannerOverlay("BANNER", "")
        s.push_overlay(ov)
        assert "⚠ BANNER" in self._plain(s)
        s.pop_overlay()
        assert "content line" in self._plain(s)
