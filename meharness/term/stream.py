"""LineStream —— LogUpdate 式行流式终端渲染。

块模型：
- 底部活跃块 = 响应文本(active) + 输入行，作为一个整体就地重绘。
  - 流式增长：光标上移到旧块顶部 → 逐行 `\r\x1b[K` 覆盖 → 块向下延伸，
    终端自然滚动，已提交内容上滚进原生 scrollback。
  - 收缩：先 `commit()` 把当前块固化成永久 scrollback，再从空白开始重开
    新块（规避终端"删除行"的复杂边界）。
  - 提交(commit)：把当前块视为永久，输入行移到块下方。
- 已提交内容从不重绘 → 原生滚动/选中可用。
"""

from __future__ import annotations

import os
import sys


def terminal_height() -> int:
    try:
        return os.get_terminal_size().lines
    except OSError:
        return 24


class LineStream:
    def __init__(self, out=None) -> None:
        self._out = out or sys.stdout
        self._active: list[str] = []
        self._input_render = ""
        self._input_cursor = 0
        # 活跃块占用行数 = active 行数 + 1(输入行)
        self._rows_used = 1

    def _flush(self, s: str) -> None:
        self._out.write(s)
        self._out.flush()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """把输入行定位到终端底部。后续 committed 内容自然在其上方滚动。"""
        h = terminal_height()
        self._flush(f"\x1b[{h};1H")
        self._render_block()

    def restore(self) -> None:
        """退出前清掉活跃块，留下一个干净的空行。"""
        prev = self._rows_used
        parts: list[str] = []
        if prev > 1:
            parts.append(f"\x1b[{prev - 1}A")
        for _ in range(prev):
            parts.append("\r\x1b[K\n")
        self._flush("".join(parts))
        self._rows_used = 1
        self._active = []

    # ------------------------------------------------------------------
    # 渲染 API
    # ------------------------------------------------------------------

    def set_input(self, text: str, cursor: int = 0, prompt: str = "❯ ") -> None:
        self._input_render = prompt + text
        self._input_cursor = cursor
        self._render_block()

    def set_response(self, lines: list[str]) -> None:
        """更新响应文本（流式增长在块内原地重绘）。

        若比当前块短（收缩），先把当前块固化提交，再从空白重开——
        避免终端"删除行"的复杂边界。
        若超过可视高度上限，把溢出部分固化成已提交文本，块内只保留尾部。
        """
        cap = max(1, terminal_height() - 3)
        if len(lines) > cap:
            self.commit()
            overflow = lines[:-cap]
            lines = lines[-cap:]
            self.commit_text("\n".join(overflow))
        if len(lines) < len(self._active):
            self.commit()
        self._active = list(lines)
        self._render_block()

    def commit(self) -> None:
        """把当前响应文本固化为永久 scrollback：清掉输入行、下移重绘。

        响应文本已经显示在屏幕上，清掉输入行后它自然成为上方滚动区内容；
        输入行在下方重绘。不产生空行、不残留输入副本。
        """
        if self._active:
            self._flush("\r\x1b[K\n")
            self._active = []
        self._rows_used = 1
        self._render_block()

    def commit_text(self, text: str) -> None:
        """把一段完成的文本写进滚动区（系统消息、banner、完成行等）。"""
        text = text.rstrip("\n")
        if not text:
            return
        self.commit()
        self._active = text.split("\n")
        self._render_block()
        self.commit()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _render_block(self) -> None:
        lines = list(self._active) + [self._input_render]
        n = len(lines)
        prev = self._rows_used
        parts: list[str] = []
        # 1. 上移到旧块顶部（旧 active 行数 = prev - 1）
        if prev > 1:
            parts.append(f"\x1b[{prev - 1}A")
        # 2. 逐行覆盖；块增长时向下延伸，终端自然滚动
        for i in range(n):
            parts.append("\r\x1b[K" + lines[i])
            if i < n - 1:
                parts.append("\n")
        # 3. 防御：块变短时清掉多出的行（正常路径由 set_response 先 commit）
        if n < prev:
            parts.append(f"\x1b[{prev - n}B")
            for _ in range(prev - n):
                parts.append("\r\x1b[K\n")
            parts.append(f"\x1b[{prev - n}A")
        # 4. 水平定位到输入行光标
        parts.append(f"\x1b[{self._input_cursor + 1}G")
        self._flush("".join(parts))
        self._rows_used = n
