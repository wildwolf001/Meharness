"""ANSI 转义与轻量 markdown 渲染。够用即可，不追求完整 markdown。"""

from __future__ import annotations

import re
import sys


def setup_utf8() -> None:
    """把 stdout/stderr 重配置为 UTF-8（现代终端默认），避免 CJK/特殊符号
    触发 GBK 编码错误。在应用启动时调用一次。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# 基础样式
RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
ITALIC = "\x1b[3m"
UNDERLINE = "\x1b[4m"
REVERSE = "\x1b[7m"

# 前景色
FG_BLACK = "\x1b[30m"
FG_RED = "\x1b[31m"
FG_GREEN = "\x1b[32m"
FG_YELLOW = "\x1b[33m"
FG_BLUE = "\x1b[34m"
FG_MAGENTA = "\x1b[35m"
FG_CYAN = "\x1b[36m"
FG_WHITE = "\x1b[37m"
FG_BRIGHT_BLACK = "\x1b[90m"
FG_BRIGHT_RED = "\x1b[91m"
FG_BRIGHT_GREEN = "\x1b[92m"
FG_BRIGHT_YELLOW = "\x1b[93m"
FG_BRIGHT_BLUE = "\x1b[94m"
FG_BRIGHT_MAGENTA = "\x1b[95m"
FG_BRIGHT_CYAN = "\x1b[96m"
FG_BRIGHT_WHITE = "\x1b[97m"

# 终端控制
CURSOR_UP = "\x1b[{n}A"
CURSOR_DOWN = "\x1b[{n}B"
CURSOR_FWD = "\x1b[{n}C"
CURSOR_BACK = "\x1b[{n}D"
CURSOR_COL = "\x1b[{n}G"
CLEAR_LINE = "\x1b[K"
CLEAR_SCREEN = "\x1b[2J"
CARRIAGE = "\r"

# 行首清行（回到行首 + 清到行尾）——用于活跃区覆盖重绘，避免残影。
CLEAR_ROW = CARRIAGE + CLEAR_LINE


def move_up(n: int) -> str:
    return CURSOR_UP.format(n=n)


def clear_row() -> str:
    return CLEAR_ROW


def cursor_col(n: int) -> str:
    return CURSOR_COL.format(n=n)


_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def strip_markdown(text: str) -> str:
    """把 markdown 链接 `[text](url)` 还原为 `text`；其余保持原样。

    避免引入解析 bug：只做这一处最低风险替换，保证输出可复制、可读。
    """
    if "[" not in text or "](" not in text:
        return text
    return _LINK_RE.sub(r"\1", text)


def styled(text: str, *codes: str) -> str:
    """给一段文本包上样式（用完后 reset）。"""
    if not codes or not text:
        return text
    return "".join(codes) + text + RESET

# ---------------------------------------------------------------------------
# 全屏模式（alt screen buffer）与光标
# ---------------------------------------------------------------------------

ALT_SCREEN_ENTER = "\x1b[?1049h"
ALT_SCREEN_EXIT = "\x1b[?1049l"
CURSOR_SAVE = "\x1b[s"
CURSOR_RESTORE = "\x1b[u"
CURSOR_HIDE = "\x1b[?25l"
CURSOR_SHOW = "\x1b[?25h"

# 鼠标：1000=点击/释放，1002=按住期间拖拽，1006=SGR 坐标序列。
# 刻意不用 ?1003h（any-motion），Windows Terminal 不支持。
MOUSE_ENABLE = "\x1b[?1000h\x1b[?1002h\x1b[?1006h"
MOUSE_DISABLE = "\x1b[?1006l\x1b[?1002l\x1b[?1000l"

# 括号粘贴：\x1b[200~...\x1b[201~ 包裹粘贴内容，换行作为字面量送达
# （否则多行粘贴每行都会被当成 Enter 提交）。
PASTE_ENABLE = "\x1b[?2004h"
PASTE_DISABLE = "\x1b[?2004l"


def cursor_at(row: int, col: int) -> str:
    """光标定位到 (row, col)，1-based。"""
    return f"\x1b[{row};{col}H"


def fg256(n: int) -> str:
    return f"\x1b[38;5;{n}m"


def bg256(n: int) -> str:
    return f"\x1b[48;5;{n}m"


def fg_rgb(r: int, g: int, b: int) -> str:
    return f"\x1b[38;2;{r};{g};{b}m"


def bg_rgb(r: int, g: int, b: int) -> str:
    return f"\x1b[48;2;{r};{g};{b}m"
