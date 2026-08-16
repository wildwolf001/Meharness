"""轻量 markdown → Seg 渲染（观感参考 claude-code 的 Markdown 组件）。

流式期间以纯文本呈现（update_block_append 增量续写，保证流畅）；回合结束
（TurnComplete）时用 render_markdown 把该回合全文重排成带样式的块：
标题加粗、行内代码变色、代码块底色、加粗/斜体、引用竖条、表格表头加粗。

刻意不做完整 GFM：覆盖模型回复里最常见的结构（标题/代码/粗斜体/列表/
引用/表格），未知结构按纯文本透传。
"""

from __future__ import annotations

import re

from meharness.term.ansi import BOLD, DIM, ITALIC, UNDERLINE, bg256, fg256
from meharness.term.screen import Seg

# 品牌色（对齐 repl/app.py 的 assistant 前缀色）
_BRAND = fg256(173)
# 行内代码前景（浅灰偏暖）
_CODE_FG = fg256(246)
# 代码块背景（接近黑色，区分正文）
_CODE_BG = bg256(236)
_CODE_FENCE_FG = fg256(240)
# 链接前景（蓝）
_LINK_FG = fg256(38)
# 引用竖条色
_QUOTE_FG = fg256(240)

_HEADING_RE = re.compile(r"^(#{1,6})\s*(.*)$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_HR_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")

# 行内格式：**粗** / *斜* / `代码` / [文本](链接)。用 capture group 拆分。
_INLINE_RE = re.compile(r"(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`|\[[^\]]+\]\([^)]+\))")


def _is_table_sep(line: str) -> bool:
    """表格分隔行：`| --- | :--: |` 且至少一个冒号/横线组合。"""
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")):
        return False
    if "---" not in s and ":::" not in s:
        return False
    return bool(_TABLE_SEP_RE.match(line))


def _inline(text: str) -> list[Seg]:
    """行内格式解析：粗体/斜体/行内代码/链接。"""
    parts = _INLINE_RE.split(text)
    segs: list[Seg] = []
    for part in parts:
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            segs.append(Seg(part[2:-2], attrs=BOLD))
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            segs.append(Seg(part[1:-1], attrs=ITALIC))
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            segs.append(Seg(part[1:-1], fg=_CODE_FG))
        else:
            m = re.match(r"^\[([^\]]+)\]\(([^)]+)\)$", part)
            if m:
                segs.append(Seg(m.group(1), fg=_LINK_FG, attrs=UNDERLINE))
            else:
                segs.append(Seg(part))
    return segs or [Seg("")]


def _emit_line(segs: list[Seg], line_segs: list[Seg], *, prefix: str = "") -> None:
    if prefix:
        segs.append(Seg(prefix))
    segs.extend(line_segs)
    segs.append(Seg("\n"))


def _code_block(text: str) -> list[Seg]:
    segs: list[Seg] = []
    segs.append(Seg("```", fg=_CODE_FENCE_FG))
    segs.append(Seg("\n"))
    for line in text.rstrip("\n").split("\n"):
        segs.append(Seg(line, fg=fg256(252), bg=_CODE_BG))
        segs.append(Seg("\n"))
    segs.append(Seg("```", fg=_CODE_FENCE_FG))
    segs.append(Seg("\n"))
    return segs


def render_markdown(text: str) -> list[Seg]:
    """把 markdown 文本渲染成带样式的 Seg 列表（含换行）。"""
    if not text:
        return []
    segs: list[Seg] = []
    lines = text.split("\n")
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        stripped = line.strip()

        # 围栏代码块
        m = re.match(r"^```", stripped)
        if m:
            buf: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # 跳过结尾 ```
            segs.extend(_code_block("\n".join(buf)))
            continue

        # 标题
        m = _HEADING_RE.match(line)
        if m:
            depth = len(m.group(1))
            attrs = BOLD + UNDERLINE if depth == 1 else BOLD
            inner = _inline(m.group(2))
            for s in inner:
                s.attrs += attrs
            _emit_line(segs, inner)
            i += 1
            continue

        # 引用
        m = _QUOTE_RE.match(line)
        if m:
            inner = _inline(m.group(1))
            for s in inner:
                s.attrs += ITALIC
                s.attrs += DIM
            _emit_line(segs, inner, prefix="│ ")
            i += 1
            continue

        # 分隔线
        if _HR_RE.match(line):
            _emit_line(segs, [Seg("────────────────", fg=_QUOTE_FG)])
            i += 1
            continue

        # 表格：连续 | 行，表头加粗、分隔行弱化
        if stripped.startswith("|") and i + 1 < n and _is_table_sep(lines[i + 1]):
            header = _inline(line)
            for s in header:
                if s.text and not s.attrs:
                    s.attrs += BOLD
            _emit_line(segs, header)
            i += 1  # 分隔行：弱化
            sep = _inline(lines[i])
            for s in sep:
                s.fg = _QUOTE_FG
            _emit_line(segs, sep)
            i += 1
            while i < n and lines[i].strip().startswith("|") and lines[i].strip() != "|":
                _emit_line(segs, _inline(lines[i]))
                i += 1
            continue

        # 列表项
        m = _LIST_RE.match(line)
        if m:
            indent = m.group(1)
            marker = m.group(2)
            rest = m.group(3)
            _emit_line(segs, _inline(rest), prefix=indent + marker + " ")
            i += 1
            continue

        # 空行
        if not stripped:
            i += 1
            continue

        # 普通段落
        _emit_line(segs, _inline(line))
        i += 1

    return segs
