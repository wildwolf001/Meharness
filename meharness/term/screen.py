"""FullscreenScreen —— 自研全屏重绘 TUI 渲染器（观感参考 Claude Code）。

模型：
- ``Seg``：一段带样式的文本（fg/bg/attrs 存 ANSI 码），宽度按纯文本 CJK 2 格算。
- ``Line``：一行渲染结果（segs + 纯文本 plain + 所属 block）。``plain`` 供选区复制。
- ``buffer``：全部内容行（消息/工具/系统），viewport 只显示其中一段。
- 增量重绘：缓存每屏幕行最近渲染串，diff 后只对变化行发
  ``\\x1b[{r};1H\\x1b[K{line}``——绝不 ``\\x1b[2J``，流式只重绘底部块+状态栏，无闪屏。

布局：内容区 [1..H-2]，状态栏 H-1，输入行 H。鼠标/选区通过挂到
``self.selection`` 的对象（duck-typed，见 ui/selection.py）在渲染时应用。
"""

from __future__ import annotations

import os
import sys
import time as _time
from dataclasses import dataclass, field
from typing import Callable, TextIO

from meharness.term.ansi import (
    ALT_SCREEN_ENTER,
    ALT_SCREEN_EXIT,
    CLEAR_LINE,
    CURSOR_HIDE,
    CURSOR_SHOW,
    CURSOR_SAVE,
    CURSOR_RESTORE,
    DIM,
    MOUSE_DISABLE,
    MOUSE_ENABLE,
    PASTE_DISABLE,
    PASTE_ENABLE,
    RESET,
    REVERSE,
    cursor_at,
)


# ---------------------------------------------------------------------------
# 格宽（CJK / 全角 = 2 格）
# ---------------------------------------------------------------------------

def cell_width(ch: str) -> int:
    o = ord(ch)
    if (
        0x4E00 <= o <= 0x9FFF
        or 0x3400 <= o <= 0x4DBF
        or 0x20000 <= o <= 0x2A6DF
        or 0xF900 <= o <= 0xFAFF
        or 0xFF00 <= o <= 0xFFEF
        or 0xAC00 <= o <= 0xD7AF
        or 0x3040 <= o <= 0x30FF
    ):
        return 2
    return 1


def display_width(text: str) -> int:
    return sum(cell_width(c) for c in text)


def cell_wrap(text: str, width: int) -> list[str]:
    """按显示格宽把文本折成多行（CJK=2 格）。"""
    if width <= 0:
        return []
    lines: list[str] = []
    cur = ""
    w = 0
    for ch in text:
        cw = cell_width(ch)
        if w + cw > width:
            if cur:
                lines.append(cur)
            cur = ch
            w = cw
        else:
            cur += ch
            w += cw
    if cur:
        lines.append(cur)
    return lines


# ---------------------------------------------------------------------------
# 样式段 / 行
# ---------------------------------------------------------------------------

@dataclass
class Seg:
    text: str
    fg: str = ""
    bg: str = ""
    attrs: str = ""


@dataclass
class Line:
    segs: list[Seg]
    plain: str
    block_id: str


def segs_to_plain(segs: list[Seg]) -> str:
    return "".join(s.text for s in segs)


def render_segs(segs: list[Seg]) -> str:
    parts: list[str] = []
    for s in segs:
        prefix = s.attrs + s.fg + s.bg
        parts.append(prefix + s.text)
    if parts:
        parts.append(RESET)
    return "".join(parts)


def _apply_sgr(codes: str, fg: str, bg: str, attrs: str) -> tuple[str, str, str]:
    """解析一条 SGR 参数串（如 `1;38;5;173`），返回更新后的 (fg, bg, attrs)。"""
    parts = codes.split(";")
    i = 0
    while i < len(parts):
        p = parts[i]
        if p in ("", "0"):
            fg = bg = attrs = ""
        elif p == "1":
            attrs += "\x1b[1m"
        elif p == "2":
            attrs += "\x1b[2m"
        elif p == "3":
            attrs += "\x1b[3m"
        elif p == "7":
            attrs += "\x1b[7m"
        elif p == "39":
            fg = ""
        elif p == "49":
            bg = ""
        elif p.isdigit() and 30 <= int(p) <= 37:
            fg = f"\x1b[{p}m"
        elif p.isdigit() and 90 <= int(p) <= 97:
            fg = f"\x1b[{p}m"
        elif p == "38" and i + 1 < len(parts):
            if parts[i + 1] == "5" and i + 2 < len(parts):
                fg = f"\x1b[38;5;{parts[i + 2]}m"
                i += 2
            elif parts[i + 1] == "2" and i + 4 < len(parts):
                fg = f"\x1b[38;2;{parts[i + 2]};{parts[i + 3]};{parts[i + 4]}m"
                i += 4
        elif p == "48" and i + 1 < len(parts):
            if parts[i + 1] == "5" and i + 2 < len(parts):
                bg = f"\x1b[48;5;{parts[i + 2]}m"
                i += 2
            elif parts[i + 1] == "2" and i + 4 < len(parts):
                bg = f"\x1b[48;2;{parts[i + 2]};{parts[i + 3]};{parts[i + 4]}m"
                i += 4
        i += 1
    return fg, bg, attrs


def parse_ansi(text: str) -> list[Seg]:
    """把带 ANSI 码的字符串解析回 Seg 列表（兼容 commit_text 的旧调用）。"""
    if "\x1b" not in text:
        return [Seg(text=text)]
    segs: list[Seg] = []
    fg = ""
    bg = ""
    attrs = ""
    buf: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\x1b" and i + 1 < n and text[i + 1] == "[":
            j = i + 2
            while j < n and text[j] != "m":
                j += 1
            if j < n:
                if buf:
                    segs.append(Seg("".join(buf), fg=fg, bg=bg, attrs=attrs))
                    buf = []
                fg, bg, attrs = _apply_sgr(text[i + 2 : j], fg, bg, attrs)
                i = j + 1
                continue
        buf.append(ch)
        i += 1
    if buf:
        segs.append(Seg("".join(buf), fg=fg, bg=bg, attrs=attrs))
    return segs


# ---------------------------------------------------------------------------
# FullscreenScreen
# ---------------------------------------------------------------------------

class Overlay:
    """内容区之上的一层临时 UI（面板/横幅），参考 Claude Code 的交互层。

    渲染：``lines(width)`` 返回要覆写到内容区的行；``anchor_top`` 决定从哪行
    开始。**交互面板默认贴底**（紧靠输入框上方/上下文末尾，参考 claude 的
    命令面板和 AskUserQuestion 就地弹出），横幅（``centered()`` 返回 False）
    锚顶。按键优先路由给栈顶 overlay（``on_key`` 返回 True 表示消费）。
    overlay 行不进 buffer，滚动/选区不受影响。
    """

    def lines(self, width: int) -> list[str]:
        raise NotImplementedError

    def centered(self) -> bool:
        return True

    def anchor_top(self, content_h: int, width: int) -> int:
        ls = self.lines(width)
        if self.centered():
            # 交互面板：贴内容区底部（最后一行对齐内容区底，紧靠输入框上方）。
            # 之前居中导致面板悬浮在屏幕中间、和正在阅读的上下文脱节。
            return max(1, content_h - len(ls) + 1)
        return 1

    async def on_key(self, key) -> bool:
        """消费按键返回 True；未消费返回 False。"""
        return False


def _detect_truecolor() -> bool:
    if os.environ.get("COLORTERM") in ("truecolor", "24bit"):
        return True
    if os.environ.get("WT_SESSION"):  # Windows Terminal
        return True
    return False


class FullscreenScreen:
    def __init__(
        self,
        out: TextIO | None = None,
        selection: object | None = None,
    ) -> None:
        self._out = out or sys.stdout
        self.selection = selection  # duck-typed: render_line(line, row) -> line | None
        self._buffer: list[Line] = []
        self._blocks: list[tuple[str, int]] = []  # (block_id, start_row)
        self._scroll_offset = 0
        self._follow_bottom = True
        self._input_text = ""
        self._input_cursor = 0
        self._input_prompt = "❯ "
        self._input_height = 1  # 多行输入占用的视觉行数（render 时更新）
        self._status_text = ""
        self._screen_rows: list[str] = []
        self._entered = False
        self._cols, self._rows = self._size()
        # 终端尺寸节流：render 每 0.5s 最多 re-fetch 一次 os.get_terminal_size()，
        # 避免流式每 token 都发一次 syscall（之前是 render() 每次都调 _size()）。
        self._last_size_check = 0.0
        self._overlays: list[Overlay] = []

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def _size(self) -> tuple[int, int]:
        try:
            cols, rows = os.get_terminal_size()
        except OSError:
            cols, rows = 80, 24
        return max(20, cols), max(8, rows)

    def enter(self) -> None:
        self._cols, self._rows = self._size()
        self._out.write(
            CURSOR_SAVE + ALT_SCREEN_ENTER + CURSOR_HIDE + MOUSE_ENABLE + PASTE_ENABLE
        )
        self._screen_rows = []  # 强制全量重绘
        self.render()
        self._entered = True

    def exit(self) -> None:
        self._out.write(
            MOUSE_DISABLE + PASTE_DISABLE + CURSOR_SHOW + ALT_SCREEN_EXIT
            + CURSOR_RESTORE
        )
        self._out.flush()
        self._entered = False

    # ------------------------------------------------------------------
    # 内容：追加 / 流式更新 / 状态
    # ------------------------------------------------------------------

    def append_block(self, segs: list[Seg], block_id: str | None = None) -> str:
        if block_id is None:
            block_id = f"b{len(self._blocks)}"
        lines = self._wrap(segs)
        start = len(self._buffer)
        self._buffer.extend(lines)
        self._blocks.append((block_id, start))
        self._auto_scroll()
        self.render()
        return block_id

    def update_block(self, block_id: str, segs: list[Seg]) -> None:
        """流式：按 block_id 精确替换该块的行区间（后续 block 平移保留）。

        相比 update_last_block，即使块后面已有工具行/系统行，也能正确续写。"""
        idx = self._block_index(block_id)
        if idx is None:
            self.append_block(segs, block_id=block_id)
            return
        start = self._blocks[idx][1]
        end = self._blocks[idx + 1][1] if idx + 1 < len(self._blocks) else len(self._buffer)
        old_len = end - start
        del self._buffer[start:end]
        new_lines = self._wrap(segs)
        self._buffer[start:start] = new_lines
        delta = len(new_lines) - old_len
        for i in range(idx + 1, len(self._blocks)):
            self._blocks[i] = (self._blocks[i][0], self._blocks[i][1] + delta)
        self._auto_scroll()
        self.render()

    def update_last_block(self, segs: list[Seg]) -> None:
        """流式：原地更新最后一个 block（兼容旧调用）。"""
        if not self._blocks:
            self.append_block(segs)
            return
        self.update_block(self._blocks[-1][0], segs)

    def update_block_append(self, block_id: str, segs: list[Seg]) -> None:
        """增量续写 block：只折新到达的 segs，追加到块末尾（O(delta)）。

        流式高频场景下，``update_block`` 每来一个 token 就把整块重折一次
        （O(块长)），总开销是 O(n²)。本方法只处理增量字符，总开销 O(n)。
        ``\\n`` 在续写时正确切新行。后续 block 的起始行随新增行数平移。
        """
        if not segs:
            return
        idx = self._block_index(block_id)
        if idx is None:
            self.append_block(segs, block_id=block_id)
            return
        start = self._blocks[idx][1]
        end = self._blocks[idx + 1][1] if idx + 1 < len(self._blocks) else len(self._buffer)
        old_len = end - start
        if old_len == 0:
            new_lines = self._wrap(segs)
            delta = len(new_lines)
        else:
            replacement, extra = self._wrap_continuation(self._buffer[end - 1], segs)
            new_lines = []
            if replacement is not None:
                new_lines.append(replacement)
            new_lines.extend(extra)
            # 保留前 old_len-1 行 + 续写 new_lines，块总长 = old_len-1+len(new_lines)，
            # 相对旧块只变了 len(new_lines)-1 行（不是 len(new_lines)-old_len，那会
            # 让后续 block 起始行错位重叠 → 流式续写把 buffer 写乱，表现为回复碎片乱排）。
            delta = len(new_lines) - 1
        keep = self._buffer[start : end - 1] if old_len > 0 else []
        self._buffer[start:end] = keep + new_lines
        for i in range(idx + 1, len(self._blocks)):
            self._blocks[i] = (self._blocks[i][0], self._blocks[i][1] + delta)
        self._auto_scroll()
        self.render()

    def _wrap_continuation(
        self, last: Line, segs: list[Seg]
    ) -> tuple[Line | None, list[Line]]:
        """在 last 行基础上续写 segs。返回 (last 行替换, 追加的新行列表)。

        - 所有新字符都能续进 last 行 → (扩展后的 last, [])。
        - 发生折行/换行 → (None, [新行...])，其中第一个新行是断行后的旧行。
        """
        width = self._cols
        cur_segs = list(last.segs)
        cur_w = display_width(last.plain)
        extra: list[Line] = []
        used_last = True  # cur_segs 仍是 last 的延续（尚未断行）
        for seg in segs:
            for ch in seg.text:
                if ch == "\n":
                    if cur_segs:
                        extra.append(
                            Line(segs=cur_segs, plain=segs_to_plain(cur_segs), block_id="")
                        )
                        cur_segs = []
                        cur_w = 0
                        used_last = False
                    continue
                cw = cell_width(ch)
                if cur_w + cw > width and cur_segs:
                    extra.append(
                        Line(segs=cur_segs, plain=segs_to_plain(cur_segs), block_id="")
                    )
                    cur_segs = []
                    cur_w = 0
                    used_last = False
                cur_segs.append(Seg(ch, seg.fg, seg.bg, seg.attrs))
                cur_w += cw
        if used_last and cur_segs:
            return Line(segs=cur_segs, plain=segs_to_plain(cur_segs), block_id=last.block_id), []
        if cur_segs:
            extra.append(Line(segs=cur_segs, plain=segs_to_plain(cur_segs), block_id=""))
        return None, extra

    def _block_index(self, block_id: str) -> int | None:
        for i, (bid, _) in enumerate(self._blocks):
            if bid == block_id:
                return i
        return None

    def set_status(self, text: str, render: bool = True) -> None:
        self._status_text = text
        if render:
            self.render()

    def set_input(self, text: str, cursor: int, prompt: str = "❯ ") -> None:
        self._input_text = text
        self._input_cursor = cursor
        self._input_prompt = prompt
        self.render()

    # -- 兼容 LineStream API（供 ReplApp 旧调用点渐进迁移）------------

    def start(self) -> None:
        self.enter()

    def restore(self) -> None:
        self.exit()

    def commit_text(self, text: str) -> None:
        """追加一段已完成文本（ANSI → Segs）。"""
        self.append_block(parse_ansi(text))

    def set_response(self, lines: list[str]) -> None:
        """流式：把最后 block 更新为这些行（ANSI → Segs）。"""
        self.update_last_block(parse_ansi("\n".join(lines)))

    def commit(self) -> None:
        """全屏无 commit 概念：结束当前流式 block，后续 append 开新块。"""
        pass

    def scroll(self, delta: int) -> None:
        content_h = self._content_height()
        max_offset = max(0, len(self._buffer) - content_h)
        self._scroll_offset = min(max_offset, max(0, self._scroll_offset + delta))
        self._follow_bottom = self._scroll_offset >= max_offset
        self.render()

    def push_overlay(self, ov: Overlay) -> None:
        self._overlays.append(ov)
        self.render()

    def pop_overlay(self) -> Overlay | None:
        ov = self._overlays.pop() if self._overlays else None
        self.render()
        return ov

    def remove_overlay(self, ov: Overlay) -> None:
        """从栈中移除指定 overlay（不限于栈顶）。banner 等非 modal overlay
        可能压在交互 overlay 之上，按键路由到下层后需要能精确弹走目标层。"""
        try:
            self._overlays.remove(ov)
            self.render()
        except ValueError:
            pass

    def overlay_below(self, ov: Overlay) -> Overlay | None:
        """栈中紧邻 ov 下方的 overlay；ov 不在栈中或已在栈底返回 None。"""
        try:
            i = self._overlays.index(ov)
        except ValueError:
            return None
        return self._overlays[i - 1] if i > 0 else None

    def top_overlay(self) -> Overlay | None:
        return self._overlays[-1] if self._overlays else None

    def scroll_top(self) -> None:
        self._scroll_offset = 0
        self._follow_bottom = False
        self.render()

    def scroll_bottom(self) -> None:
        self._follow_bottom = True
        self._auto_scroll()
        self.render()

    def lines(self) -> list[Line]:
        """当前 buffer 全部行（选区/复制用）。"""
        return self._buffer

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _content_height(self) -> int:
        # 多行输入会占用底部若干行，内容区相应缩小
        il = getattr(self, "_input_height", 1)
        return max(1, self._rows - 1 - il)

    def _wrap(self, segs: list[Seg]) -> list[Line]:
        """跨 Seg 折行：逐字符按显示宽切分，原地分裂 Seg。

        显式 ``\\n`` 被当作真正的换行（切出新行），不再当成 1 格宽字符混进行内
        —— 否则多行内容渲染时终端下移，与屏幕行号记账错位，后续行全部顶乱。
        """
        width = self._cols
        result: list[Line] = []
        cur_segs: list[Seg] = []
        cur_w = 0
        for seg in segs:
            for ch in seg.text:
                if ch == "\n":
                    result.append(Line(segs=cur_segs, plain=segs_to_plain(cur_segs), block_id=""))
                    cur_segs = []
                    cur_w = 0
                    continue
                cw = cell_width(ch)
                if cur_w + cw > width:
                    result.append(Line(segs=cur_segs, plain=segs_to_plain(cur_segs), block_id=""))
                    cur_segs = []
                    cur_w = 0
                cur_segs.append(Seg(ch, seg.fg, seg.bg, seg.attrs))
                cur_w += cw
        if cur_segs:
            result.append(Line(segs=cur_segs, plain=segs_to_plain(cur_segs), block_id=""))
        if not result:
            result.append(Line(segs=[], plain="", block_id=""))
        return result

    def _auto_scroll(self) -> None:
        content_h = self._content_height()
        max_offset = max(0, len(self._buffer) - content_h)
        if self._follow_bottom or self._scroll_offset > max_offset:
            self._scroll_offset = max_offset
            self._follow_bottom = True

    def _buffer_row_at_screen(self, r: int) -> int | None:
        """屏幕内容区行 r (1..content_h) → buffer 行索引；空区返回 None。"""
        content_h = self._content_height()
        if r < 1 or r > content_h:
            return None
        idx = self._scroll_offset + r - 1
        if idx < 0 or idx >= len(self._buffer):
            return None
        return idx

    def _render_line(self, line: Line, buffer_row: int) -> str:
        if self.selection is not None and getattr(self.selection, "render_line", None):
            styled = self.selection.render_line(line, buffer_row)
            if styled is not None:
                return styled
        return render_segs(line.segs)

    def _render_status_line(self) -> str:
        if self._status_text:
            return DIM + self._status_text + RESET
        return ""

    def _input_layout(self) -> tuple[list[tuple[str, int, int]], int, int]:
        """多行输入布局：视觉行 + 光标位置。

        返回 (visual, caret_row, caret_char)：
        - visual：[(显示文本含首行 prompt, 覆盖字符起, 覆盖字符止), ...]
        - caret_row / caret_char：光标在视觉行内的字符偏移（0-based，显示文本内）。
        """
        width = self._cols
        prompt = self._input_prompt
        prompt_w = display_width(prompt)
        text = self._input_text
        cursor = min(self._input_cursor, len(text))

        visual: list[tuple[str, int, int]] = []
        pos = 0
        for li, ln in enumerate(text.split("\n")):
            avail = width - prompt_w if li == 0 else width
            cur = ""
            w = 0
            seg_start = pos
            i = 0
            for ch in ln:
                cw = cell_width(ch)
                if w + cw > avail and cur:
                    visual.append((cur, seg_start, pos + i))
                    cur = ""
                    w = 0
                    seg_start = pos + i
                cur += ch
                w += cw
                i += 1
            visual.append((cur, seg_start, pos + len(ln)))
            pos += len(ln) + 1
        if not visual:
            visual = [(prompt, 0, 0)]
        else:
            d, cs, ce = visual[0]
            visual[0] = (prompt + d, cs, ce)

        # 光标所在视觉行 + 行内字符偏移
        caret_row = len(visual) - 1
        caret_char = len(visual[-1][0])
        for row, (disp, cs, ce) in enumerate(visual):
            if cs <= cursor <= ce:
                caret_row = row
                prompt_len = len(prompt) if row == 0 else 0
                caret_char = prompt_len + (cursor - cs)
                break
        return visual, caret_row, caret_char

    def _render_input_lines(self) -> tuple[list[str], int, int]:
        """渲染多行输入区。返回 (各行 ANSI 字符串, 光标所在行, 光标单元格列)。"""
        visual, caret_row, caret_char = self._input_layout()
        max_in = max(1, self._rows - 2)  # 输入区最多占 rows-2 行（至少留 1 内容行）
        start = 0
        if len(visual) > max_in:
            # 过高：窗口对齐到光标行（尽量把光标放底部）
            start = max(0, min(caret_row, len(visual) - max_in))
            shown = visual[start : start + max_in]
        else:
            shown = visual
        self._input_height = len(shown)

        rows: list[str] = []
        caret_col = 0
        for row, (disp, cs, ce) in enumerate(shown):
            abs_row = start + row
            if abs_row == caret_row:
                before = disp[:caret_char]
                at = disp[caret_char : caret_char + 1] or " "
                after = disp[caret_char + 1 :]
                rows.append(before + REVERSE + at + RESET + after)
                caret_col = display_width(before)
            else:
                rows.append(disp)
        return rows, caret_row - start, caret_col

    def render(self) -> None:
        # 终端尺寸节流：0.5s 内只 re-fetch 一次。render 是流式热路径
        # （每 token 一次），每次都 os.get_terminal_size() 是不必要的 syscall。
        now = _time.monotonic()
        if now - self._last_size_check >= 0.5:
            self._cols, self._rows = self._size()
            self._last_size_check = now
        # 先算输入区行数（会更新 _input_height），再定内容区高度
        input_rows, caret_abs_row, caret_col = self._render_input_lines()
        content_h = self._content_height()
        self._auto_scroll()

        new_rows: list[str] = []
        input_first = content_h + 2  # 输入区首行（1-based）
        for r in range(1, self._rows + 1):
            if r <= content_h:
                idx = self._buffer_row_at_screen(r)
                if idx is not None:
                    new_rows.append(self._render_line(self._buffer[idx], idx))
                else:
                    new_rows.append("")
            elif r == content_h + 1:
                new_rows.append(self._render_status_line())
            else:  # 输入区（多行，底对齐）
                new_rows.append(input_rows[r - input_first])

        # overlay 层：按入栈顺序逐层覆写内容区（后入者最上层）。
        # 覆写发生在 diff 之前，增量重绘天然保留；overlay 行不进 buffer。
        for ov in self._overlays:
            top = ov.anchor_top(content_h, self._cols)
            for i, line in enumerate(ov.lines(self._cols)):
                r = top + i
                if 1 <= r <= content_h:
                    new_rows[r - 1] = line

        parts: list[str] = []
        for r in range(1, self._rows + 1):
            line = new_rows[r - 1]
            prev = self._screen_rows[r - 1] if r - 1 < len(self._screen_rows) else None
            if prev == line:
                continue
            parts.append(cursor_at(r, 1) + CLEAR_LINE + line)

        # 光标定位到输入区光标处（多行时在光标所在行）
        if parts:
            self._out.write("".join(parts))
        caret_row = input_first + caret_abs_row
        self._out.write(cursor_at(caret_row, min(self._cols, caret_col + 1)))
        self._out.flush()
        self._screen_rows = new_rows
