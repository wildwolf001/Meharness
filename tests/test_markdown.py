# markdown → Seg 渲染器测试（观感参考 claude-code）。

from __future__ import annotations

from meharness.markdown import render_markdown
from meharness.term.ansi import BOLD, DIM, ITALIC, UNDERLINE, bg256
from meharness.term.screen import Seg


def _plain(segs: list[Seg]) -> str:
    return "".join(s.text for s in segs)


def test_heading_strips_hash_and_bolds() -> None:
    segs = render_markdown("## 架构\n正文")
    assert _plain(segs) == "架构\n正文\n"
    # 标题行加粗
    title = next(s for s in segs if s.text == "架构")
    assert BOLD in title.attrs


def test_h1_underlined() -> None:
    segs = render_markdown("# 一级标题\n")
    assert UNDERLINE in segs[0].attrs and BOLD in segs[0].attrs


def test_code_block_bg_and_fence() -> None:
    segs = render_markdown("```py\nprint(1)\n```\n")
    plain = _plain(segs)
    assert "print(1)" in plain
    code_line = next(s for s in segs if "print" in s.text)
    assert bg256(236) in code_line.bg


def test_inline_bold_italic_code() -> None:
    segs = render_markdown("**粗体** *斜体* `code` 普通\n")
    plain = _plain(segs)
    assert "粗体" in plain and "斜体" in plain and "code" in plain
    bold = next(s for s in segs if s.text == "粗体")
    assert BOLD in bold.attrs
    italic = next(s for s in segs if s.text == "斜体")
    assert ITALIC in italic.attrs
    code = next(s for s in segs if s.text == "code")
    assert code.fg  # 行内代码有前景色


def test_list_item_marker_preserved() -> None:
    segs = render_markdown("- **后端**：REST\n- 待办\n")
    assert _plain(segs).startswith("- 后端：REST\n- 待办\n")
    bold = next(s for s in segs if s.text == "后端")
    assert BOLD in bold.attrs


def test_blockquote_bar_and_italic() -> None:
    segs = render_markdown("> 引用内容\n")
    assert _plain(segs).startswith("│ ")
    quote = next(s for s in segs if s.text == "引用内容")
    assert ITALIC in quote.attrs and DIM in quote.attrs


def test_table_header_bold() -> None:
    segs = render_markdown("| 项目 | Stars |\n|---|----|\n| A | 1 |\n")
    plain = _plain(segs)
    assert "项目" in plain and "Stars" in plain and "A" in plain
    header = next(s for s in segs if "项目" in s.text)
    assert BOLD in header.attrs


def test_plain_text_passthrough() -> None:
    segs = render_markdown("简单的一句话。\n")
    assert _plain(segs) == "简单的一句话。\n"
    assert all(not s.attrs and not s.fg and not s.bg for s in segs)


def test_link_renders_text() -> None:
    segs = render_markdown("[链接文本](https://x.com)\n")
    assert _plain(segs).startswith("链接文本")
