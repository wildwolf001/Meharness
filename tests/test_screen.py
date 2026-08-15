# FullscreenScreen 全屏渲染器单测：enter/exit 序列、增量重绘（无 2J）、
# viewport 滚动钳位、CJK 折行、选区反色与明文提取。

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest

from meharness.term.ansi import ALT_SCREEN_ENTER, CURSOR_SAVE, REVERSE
from meharness.term.screen import FullscreenScreen, Seg, display_width, parse_ansi
from meharness.ui.selection import SelectionState, extract_selected_text


@pytest.fixture()
def term24():
    with patch(
        "meharness.term.screen.os.get_terminal_size", return_value=(80, 24)
    ):
        yield


def _mk() -> tuple[StringIO, FullscreenScreen]:
    buf = StringIO()
    return buf, FullscreenScreen(out=buf)


def test_enter_sequence(term24) -> None:
    buf, scr = _mk()
    scr.enter()
    out = buf.getvalue()
    assert out.startswith(CURSOR_SAVE)
    assert ALT_SCREEN_ENTER in out
    assert "\x1b[?1006h" in out  # SGR 鼠标已启用
    assert "\x1b[?25l" in out  # 光标隐藏


def test_exit_restores(term24) -> None:
    buf, scr = _mk()
    scr.enter()
    buf.seek(0)
    buf.truncate(0)
    scr.exit()
    out = buf.getvalue()
    assert "\x1b[?1049l" in out
    assert "\x1b[?25h" in out


def test_append_never_emits_clear_screen(term24) -> None:
    buf, scr = _mk()
    scr.enter()
    for i in range(10):
        buf.seek(0)
        buf.truncate(0)
        scr.append_block([Seg(f"line {i}")])
        assert "\x1b[2J" not in buf.getvalue()


def test_append_grows_buffer(term24) -> None:
    _, scr = _mk()
    scr.enter()
    scr.append_block([Seg("hello")])
    scr.append_block([Seg("world")])
    assert len(scr._buffer) == 2
    assert scr._buffer[1].plain == "world"


def test_viewport_scroll_clamp(term24) -> None:
    _, scr = _mk()
    scr.enter()
    # 内容区高度 = 24 - 2 = 22
    for i in range(25):
        scr.append_block([Seg(f"line {i}")])
    assert scr._scroll_offset == 3  # auto-follow bottom
    scr.scroll(-10)
    assert scr._scroll_offset == 0  # 钳位到顶
    scr.scroll(100)
    assert scr._scroll_offset == 3  # 钳位到底


def test_short_content_no_scroll(term24) -> None:
    _, scr = _mk()
    scr.enter()
    scr.append_block([Seg("only one line")])
    assert scr._scroll_offset == 0


def test_cjk_wrap(term24) -> None:
    _, scr = _mk()
    scr.enter()
    text = "你" * 50  # 100 格，80 格/行 → 2 行
    scr.append_block([Seg(text)])
    assert len(scr._buffer) == 2
    assert display_width(scr._buffer[0].plain) == 80
    assert display_width(scr._buffer[1].plain) == 20


def test_update_block_replaces_only_that_block(term24) -> None:
    _, scr = _mk()
    scr.enter()
    b1 = scr.append_block([Seg("aaa")], block_id="assistant")
    scr.append_block([Seg("tool line")])  # 后续 block
    scr.update_block(b1, [Seg("aaa bbb ccc")])
    # assistant block 行被替换，tool line 仍在
    plains = [l.plain for l in scr._buffer]
    assert any(p == "aaa bbb ccc" for p in plains)
    assert any(p == "tool line" for p in plains)


def test_update_block_append_continues_last_line(term24) -> None:
    _, scr = _mk()
    scr.enter()
    b = scr.append_block([Seg("● ")])
    scr.update_block_append(b, [Seg("hi")])
    scr.update_block_append(b, [Seg(" there")])
    assert len(scr._buffer) == 1
    assert scr._buffer[0].plain == "● hi there"


def test_update_block_append_wrap_shifts_following_blocks(term24) -> None:
    _, scr = _mk()
    scr.enter()
    b = scr.append_block([Seg("● ")])
    scr.append_block([Seg("TOOL")])  # 后续 block
    # 80 格宽："● " 占 2 格，追加 80 个 ASCII → 补满第一行后换行
    scr.update_block_append(b, [Seg("a" * 80)])
    plains = [l.plain for l in scr._buffer]
    assert plains[0] == "● " + "a" * 78
    assert plains[1] == "aa"
    assert plains[2] == "TOOL"  # 后续 block 被平移，仍在末尾


def test_update_block_append_newline(term24) -> None:
    _, scr = _mk()
    scr.enter()
    b = scr.append_block([Seg("● ")])
    scr.update_block_append(b, [Seg("line1\nline2")])
    assert [l.plain for l in scr._buffer] == ["● line1", "line2"]


def test_multiline_input_renders_two_logical_lines(term24) -> None:
    import re

    _, scr = _mk()
    scr.enter()
    scr._cols = 20
    scr.set_input("第一行\n第二行内容", cursor=8, prompt="❯ ")
    rows, caret_row, caret_col = scr._render_input_lines()
    plain = [re.sub(r"\x1b\[[0-9;]*m", "", r) for r in rows]
    assert len(plain) == 2
    assert plain[0].startswith("❯ ") and "第一行" in plain[0]
    assert "第二行内容" in plain[1]
    assert scr._input_height == 2


def test_multiline_input_wraps_long_line(term24) -> None:
    import re

    _, scr = _mk()
    scr.enter()
    scr._cols = 12  # "❯ " 占 2 格，首行可用 10 格
    scr.set_input("123456789012345", cursor=15, prompt="❯ ")
    rows, caret_row, caret_col = scr._render_input_lines()
    plain = [re.sub(r"\x1b\[[0-9;]*m", "", r) for r in rows]
    assert len(plain) == 2
    assert "1234567890" in plain[0]  # 首行折满
    assert "12345" in plain[1]
    assert caret_row == 1
    assert caret_col == 5


def test_multiline_input_content_area_shrinks(term24) -> None:
    _, scr = _mk()
    scr.enter()
    scr._cols = 40
    scr.set_input("a\nb\nc", cursor=5, prompt="❯ ")
    rows, _, _ = scr._render_input_lines()
    assert len(rows) == 3
    # 内容区高度 = rows - 1 - input_height = 24 - 1 - 3 = 20
    assert scr._content_height() == 20


def test_update_block_append_wrapped_then_tool_keeps_order(term24) -> None:
    # 回归：块折成多行后，后续块起始行必须随块实际行数平移。
    # 旧 bug：delta = len(new_lines)-old_len 在 old_len>1 时算错（应为
    # len(new_lines)-1），后续块起始行重叠进本块 → 下次续写把 buffer 写乱，
    # 表现为回复内容碎片乱排（如 "规则吗/或调整/加功能" 无头无尾）。
    _, scr = _mk()
    scr.enter()
    b = scr.append_block([Seg("● ")])
    scr.update_block_append(b, [Seg("x" * 78)])          # 填满第一行（80 格）
    scr.update_block_append(b, [Seg("第二行内容。\n")])  # 折出第二行
    assert [l.plain for l in scr._buffer] == ["● " + "x" * 78, "第二行内容。"]
    scr.append_block([Seg("  ⏳ [tool]")])               # 后续块（块后）
    # 第一次续写：正确追加到最后一行
    scr.update_block_append(b, [Seg("第三行。")])
    assert [l.plain for l in scr._buffer] == [
        "● " + "x" * 78,
        "第二行内容。第三行。",
        "  ⏳ [tool]",
    ]
    # 第二次续写：若上一步后续块起始行没跟对，这里会把内容插到错位处
    scr.update_block_append(b, [Seg("第四行。")])
    assert [l.plain for l in scr._buffer] == [
        "● " + "x" * 78,
        "第二行内容。第三行。第四行。",
        "  ⏳ [tool]",
    ]


def test_selection_reverse_and_extract(term24) -> None:
    _, scr = _mk()
    scr.enter()
    scr.append_block([Seg("hello world")])
    sel = SelectionState()
    sel.start(0, 0)
    sel.update(0, 4)  # 显示列 [0,4] → "hello"
    rendered = sel.render_line(scr._buffer[0], 0)
    assert REVERSE in rendered
    text = extract_selected_text(scr, sel)
    assert text == "hello"


def test_selection_multiline_extract(term24) -> None:
    _, scr = _mk()
    scr.enter()
    scr.append_block([Seg("line one")])
    scr.append_block([Seg("line two")])
    sel = SelectionState()
    sel.start(0, 0)
    sel.update(1, 7)  # 跨两行
    text = extract_selected_text(scr, sel)
    assert text == "line one\nline two"


def test_commit_text_compat(term24) -> None:
    _, scr = _mk()
    scr.enter()
    scr.commit_text("system message")
    assert scr._buffer[-1].plain == "system message"


def test_set_response_compat(term24) -> None:
    _, scr = _mk()
    scr.enter()
    scr.set_response(["a", "b"])
    # "\n" 被正确切为独立行（不再当 1 格宽字符混进行内）
    assert [l.plain for l in scr._buffer] == ["a", "b"]


def test_parse_ansi_basic() -> None:
    segs = parse_ansi("\x1b[2m dim \x1b[0m plain")
    plains = "".join(s.text for s in segs)
    assert plains == " dim  plain"


def test_parse_ansi_plain_passthrough() -> None:
    segs = parse_ansi("no ansi here")
    assert len(segs) == 1
    assert segs[0].text == "no ansi here"


def test_parse_ansi_256color() -> None:
    segs = parse_ansi("\x1b[38;5;173morange\x1b[0m")
    assert segs[0].fg == "\x1b[38;5;173m"
    assert segs[0].text == "orange"
