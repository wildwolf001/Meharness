# LineStream 行流式渲染的单测：用 StringIO 捕获 ANSI 输出，
# mock 终端高度，验证原地重绘/commit/收缩/溢出/restore 的确定性行为。

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest

from meharness.term.stream import LineStream


@pytest.fixture()
def buf() -> StringIO:
    return StringIO()


@pytest.fixture()
def stream(buf: StringIO) -> LineStream:
    return LineStream(out=buf)


@pytest.fixture()
def term24():
    """mock terminal_height 为固定 24 行。"""
    with patch("meharness.term.stream.terminal_height", return_value=24):
        yield


def test_init_state(stream: LineStream) -> None:
    assert stream._rows_used == 1
    assert stream._active == []
    assert stream._input_render == ""
    assert stream._input_cursor == 0


def test_start_positions_to_bottom(buf: StringIO, stream: LineStream, term24) -> None:
    # start: 光标移到第 24 行，再渲染一个空输入行（\r\x1b[K + 光标定位）
    stream.start()
    assert buf.getvalue() == "\x1b[24;1H\r\x1b[K\x1b[1G"


def test_set_input_redraws_input_line(buf: StringIO, stream: LineStream) -> None:
    stream.set_input("hi", cursor=2, prompt="❯ ")
    # n=1（只有输入行），prev=1：\r\x1b[K + "❯ hi" + 光标定位到第 3 列
    assert buf.getvalue() == "\r\x1b[K❯ hi\x1b[3G"
    assert stream._rows_used == 1


def test_set_response_grows_block(buf: StringIO, stream: LineStream) -> None:
    stream.set_input("typing", cursor=6, prompt="❯ ")
    buf.seek(0)
    buf.truncate(0)

    stream.set_response(["line 1", "line 2"])
    # 块从 1 行（仅输入行）长到 3 行（2 响应 + 1 输入）
    assert buf.getvalue() == (
        "\r\x1b[Kline 1\n"
        "\r\x1b[Kline 2\n"
        "\r\x1b[K❯ typing\x1b[7G"
    )
    assert stream._rows_used == 3


def test_set_response_growth_redraws_in_place(buf: StringIO, stream: LineStream) -> None:
    stream.set_response(["a", "b"])
    buf.seek(0)
    buf.truncate(0)

    stream.set_response(["a", "b", "c"])
    # prev=3（2 响应 + 1 输入行），上移 2 行后逐行覆盖并向下延伸
    assert buf.getvalue() == (
        "\x1b[2A"
        "\r\x1b[Ka\n"
        "\r\x1b[Kb\n"
        "\r\x1b[Kc\n"
        "\r\x1b[K\x1b[1G"
    )
    assert stream._rows_used == 4


def test_commit_freezes_response(buf: StringIO, stream: LineStream) -> None:
    stream.set_response(["done"])
    buf.seek(0)
    buf.truncate(0)

    stream.commit()
    # 固化：换行推出已显示内容 + 在底部重绘一个空输入行
    assert buf.getvalue() == "\r\x1b[K\n\r\x1b[K\x1b[1G"
    assert stream._active == []
    assert stream._rows_used == 1


def test_shrink_commits_before_redraw(buf: StringIO, stream: LineStream) -> None:
    stream.set_response(["a", "b"])
    buf.seek(0)
    buf.truncate(0)

    # 新响应比当前块短：先 commit 固化，再重开单行块
    stream.set_response(["x"])
    out = buf.getvalue()
    assert "\r\x1b[K\n" in out  # commit 的换行
    # 新块：x + 输入行（空）两行
    assert "\r\x1b[Kx\n\r\x1b[K\x1b[1G" in out
    assert stream._active == ["x"]
    assert stream._rows_used == 2  # 1 响应 + 1 输入行


def test_overflow_commits_overflow_text(buf: StringIO, term24) -> None:
    # 高度 24 → cap = 21。传入 25 行：前 4 行溢出，应固化进 scrollback。
    stream = LineStream(out=buf)
    lines = [f"row {i}" for i in range(25)]
    stream.set_response(lines)

    # 溢出部分作为已提交文本出现（commit_text 内部固化），尾部留在活跃块
    out = buf.getvalue()
    assert "row 0" in out
    assert "row 3" in out
    assert "row 21" in out
    assert "row 24" in out
    assert stream._active == lines[-21:]
    assert stream._rows_used == 22  # 21 响应 + 1 输入行


def test_commit_text_writes_and_clears(buf: StringIO, stream: LineStream) -> None:
    stream.commit_text("hello")
    out = buf.getvalue()
    assert "\r\x1b[Khello" in out
    assert stream._active == []
    assert stream._rows_used == 1


def test_commit_text_multiline(buf: StringIO, stream: LineStream) -> None:
    stream.commit_text("l1\nl2")
    out = buf.getvalue()
    assert "\r\x1b[Kl1\n\r\x1b[Kl2" in out
    assert stream._active == []


def test_restore_clears_block(buf: StringIO, stream: LineStream) -> None:
    stream.set_response(["a", "b"])
    assert stream._rows_used == 3
    buf.seek(0)
    buf.truncate(0)

    stream.restore()
    # 上移 prev-1=2 行，清掉 3 行（\r\x1b[K\n 三次）
    assert buf.getvalue() == "\x1b[2A" + "\r\x1b[K\n" * 3
    assert stream._rows_used == 1
    assert stream._active == []
