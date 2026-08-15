"""终端"选中即复制"（全屏模式版）。

坐标用 buffer 坐标：(row, col)，row 是 screen.buffer 的行索引，col 是
**显示列**（0-based，CJK=2 格）。鼠标屏幕坐标在 app 层换算成 buffer 坐标。
渲染时命中选区的字符段加 REVERSE 反色；复制时按显示列截取 ``Line.plain``
拼明文（剥样式）。
"""

from __future__ import annotations

from meharness.term.ansi import REVERSE
from meharness.term.screen import Line, Seg, cell_width, render_segs


class SelectionState:
    __slots__ = ("active", "anchor", "current")

    def __init__(self) -> None:
        self.active = False
        self.anchor: tuple[int, int] | None = None  # (row, col)
        self.current: tuple[int, int] | None = None

    def start(self, row: int, col: int) -> None:
        self.active = True
        self.anchor = (row, col)
        self.current = (row, col)

    def update(self, row: int, col: int) -> None:
        self.current = (row, col)

    def normalized(self) -> tuple[int, int, int, int] | None:
        if not self.active or self.anchor is None or self.current is None:
            return None
        r1, c1 = self.anchor
        r2, c2 = self.current
        if (r1, c1) > (r2, c2):
            r1, c1, r2, c2 = r2, c2, r1, c1
        return (r1, c1, r2, c2)

    def clear(self) -> None:
        self.active = False
        self.anchor = None
        self.current = None

    def is_row_selected(self, row: int) -> bool:
        rect = self.normalized()
        if rect is None:
            return False
        return rect[0] <= row <= rect[2]

    def is_cell_selected(self, row: int, col: int) -> bool:
        """字符起始显示列 col 是否落在选区内。"""
        rect = self.normalized()
        if rect is None:
            return False
        r1, c1, r2, c2 = rect
        if row < r1 or row > r2:
            return False
        if r1 == r2:
            return c1 <= col <= c2
        if row == r1:
            return col >= c1
        if row == r2:
            return col <= c2
        return True

    def render_line(self, line: Line, buffer_row: int) -> str | None:
        """screen 渲染钩子：选中行把命中字符段加 REVERSE；未选中返回 None。"""
        if not self.is_row_selected(buffer_row):
            return None
        segs: list[Seg] = []
        col = 0
        for seg in line.segs:
            for ch in seg.text:
                sel = self.is_cell_selected(buffer_row, col)
                attrs = (seg.attrs + REVERSE) if sel else seg.attrs
                segs.append(Seg(ch, seg.fg, seg.bg, attrs))
                col += cell_width(ch)
        return render_segs(segs)


def _slice_plain(plain: str, col_start: int, col_end: int) -> str:
    """按显示列区间截取纯文本（CJK=2 格）。"""
    out: list[str] = []
    col = 0
    for ch in plain:
        w = cell_width(ch)
        if col >= col_end:
            break
        if col + w > col_start:
            out.append(ch)
        col += w
    return "".join(out)


def extract_selected_text(screen, state: SelectionState) -> str:
    """从 screen.buffer 按选区拼明文（多行加换行）。"""
    rect = state.normalized()
    if rect is None:
        return ""
    r1, c1, r2, c2 = rect
    lines = screen.lines()
    parts: list[str] = []
    for row in range(r1, r2 + 1):
        if row < 0 or row >= len(lines):
            parts.append("")
            continue
        plain = lines[row].plain
        if r1 == r2:
            parts.append(_slice_plain(plain, c1, c2 + 1))
        elif row == r1:
            parts.append(_slice_plain(plain, c1, 10**9))
        elif row == r2:
            parts.append(_slice_plain(plain, 0, c2 + 1))
        else:
            parts.append(plain)
    return "\n".join(parts).rstrip("\n")
