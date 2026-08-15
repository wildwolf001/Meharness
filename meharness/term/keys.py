# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
"""raw 按键读取：归一化成简单键值。

兼容：
- Windows 真实控制台(cmd / Windows Terminal ConPTY)：msvcrt 扫描码 \xe0/\x00 + VT
- mintty(git-bash pty)：msvcrt 不可用,退回 sys.stdin 读字节 + VT 序列
- Unix：termios raw 模式 + VT 序列

归一化键(元组)：
  ("enter",) ("tab",) ("shift_tab",) ("up",) ("down",) ("left",) ("right",)
  ("home",) ("end",) ("delete",) ("escape",) ("ctrl_c",) ("ctrl_o",)
  ("ctrl", <ch>) ("print", <char>) ("eof",) ("unknown",)
  ("mouse", MouseEvent) —— SGR 1006 鼠标（点击/拖拽/滚轮）
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

# CSI 最终字节 -> 键名
_CSI_MAP = {
    "A": "up",
    "B": "down",
    "C": "right",
    "D": "left",
    "H": "home",
    "F": "end",
    "Z": "shift_tab",
    "3~": "delete",
    "1~": "home",
    "4~": "end",
    "5~": "page_up",
    "6~": "page_down",
}

# legacy 控制台扫描码(msvcrt \xe0/\x00 后跟随的码)
_SCAN_MAP = {
    "H": "up",
    "P": "down",
    "K": "left",
    "M": "right",
    "G": "home",
    "O": "end",
    "S": "delete",
    "I": "page_up",
    "Q": "page_down",
    "\x0f": "shift_tab",
}


@dataclass
class MouseEvent:
    """SGR 1006 鼠标事件。x/y 为 1-based 屏幕坐标。"""

    x: int
    y: int
    button: str  # left/middle/right/motion/wheel_up/wheel_down/none
    press: bool = False
    release: bool = False
    shift: bool = False
    alt: bool = False
    ctrl: bool = False


def _parse_sgr_mouse(param: str, final: str) -> MouseEvent | None:
    """解析 SGR 1006 序列参数（`<b;x;y`）与最终字节（M/m）。"""
    body = param[1:]
    try:
        b, x, y = (int(p) for p in body.split(";"))
    except ValueError:
        return None
    button = "none"
    press = final == "M"
    release = final == "m"
    if b & 64:
        button = "wheel_up" if (b & 3) == 0 else "wheel_down"
        press, release = True, False
    elif b & 32:
        button = "motion"
        press = True
    else:
        button = {0: "left", 1: "middle", 2: "right"}.get(b & 3, "none")
    return MouseEvent(
        x=x,
        y=y,
        button=button,
        press=press,
        release=release,
        shift=bool(b & 4),
        alt=bool(b & 8),
        ctrl=bool(b & 16),
    )


class KeyReader:
    def __init__(self) -> None:
        self._stdin = sys.stdin
        self._raw = False
        self._fd = None
        self._vt_enabled = False
        self._old_console_mode: int | None = None
        self._pasting = False  # 括号粘贴模式（\x1b[200~/201~ 之间）
        self._msvcrt = self._detect_msvcrt()
        # 逃生门：MEHARNESS_FORCE_MSCRT=1 强制 msvcrt（VT 原始字节路径在个别
        # 终端/配置不兼容时退回旧行为，代价是收不到鼠标）。
        if os.environ.get("MEHARNESS_FORCE_MSCRT") != "1" and self._msvcrt:
            # Windows 控制台：优先切 VT 输入模式走 stdin 原始字节流
            # （ConPTY 下只有这样才能收到鼠标 SGR 序列——msvcrt.getwch 只读
            # 键盘事件，鼠标永远到不了它，滚轮/拖拽全失效）。失败则退回 msvcrt。
            if self._enable_windows_vt():
                self._msvcrt = False
        if self._vt_enabled:
            # VT 原始字节路径：后台线程读 stdin → 队列，取字节带超时，
            # 避免半截 VT 序列把 key pump 卡死在 buffer.read 阻塞读上
            # （os.set_blocking 在部分 Windows 运行时不可用，用线程代替）。
            self._stdin_q: "queue.Queue[int] | None" = None
            self._start_stdin_reader()
        if not self._msvcrt and sys.platform != "win32":
            self._setup_unix_raw()

    def _start_stdin_reader(self) -> None:
        """后台线程持续读 stdin 原始字节到队列（daemon，随进程退出）。"""
        import queue
        import threading

        self._stdin_q = queue.Queue()

        def _pump() -> None:
            try:
                while True:
                    data = self._stdin.buffer.read(1)
                    if not data:
                        break
                    self._stdin_q.put(data[0])
            except Exception:
                pass

        threading.Thread(target=_pump, daemon=True).start()

    @staticmethod
    def _detect_msvcrt() -> bool:
        """是否使用 msvcrt 读取按键。

        仅当存在真实 Win32 控制台时才用 msvcrt（cmd / Windows Terminal）；
        mintty（git-bash 的 cygwin pty）下 GetConsoleMode 失败，退回
        sys.stdin + VT 序列解析路径。
        """
        if sys.platform != "win32":
            return False
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
            mode = ctypes.c_uint()
            return bool(kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
        except Exception:
            return False

    def _enable_windows_vt(self) -> bool:
        """启用控制台 VT 输入模式 + 去行缓冲/回显。

        对齐 claude-code：直接读 stdin 原始字节，键盘+鼠标 SGR 一起收。
        保存旧模式供 restore。失败返回 False（调用方退回 msvcrt）。
        """
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
            mode = ctypes.c_uint()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
            new_mode = (mode.value | ENABLE_VIRTUAL_TERMINAL_INPUT) & ~0x0006
            # ~0x0006: 去掉 ENABLE_LINE_INPUT(2) + ENABLE_ECHO_INPUT(4)
            if not kernel32.SetConsoleMode(handle, new_mode):
                return False
            self._vt_enabled = True
            self._old_console_mode = mode.value
            return True
        except Exception:
            return False

    def _setup_unix_raw(self) -> None:
        try:
            import termios
            import tty

            self._fd = self._stdin.fileno()
            self._old = termios.tcgetattr(self._fd)
            tty.setraw(self._fd)
            self._raw = True
        except Exception:
            self._raw = False

    def restore_terminal(self) -> None:
        if self._vt_enabled and self._old_console_mode is not None:
            try:
                import ctypes

                kernel32 = ctypes.windll.kernel32
                handle = kernel32.GetStdHandle(-10)
                kernel32.SetConsoleMode(handle, self._old_console_mode)
            except Exception:
                pass
        if self._raw and self._fd is not None:
            try:
                import termios

                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)
            except Exception:
                pass

    # -- 底层字节读取 ---------------------------------------------------

    def _read_byte(self, wait: bool) -> int:
        """读取一个字节。wait=True 阻塞;wait=False 立即返回(-1 表示暂无)。"""
        if self._msvcrt:
            import msvcrt

            # getwch() 本身会阻塞到有按键；wait=False 时先用 kbhit() 探测，
            # 有输入才调 getwch()（立即返回）。绝不能用 `while not kbhit(): pass`
            # 忙等 —— 那会在无输入时把一个 CPU 核打满，拖慢整台机器。
            if not wait and not msvcrt.kbhit():
                return -1
            ch = msvcrt.getwch()
            return ord(ch) if isinstance(ch, str) else ch

        # stdin 路径（VT 原始字节，键盘+鼠标都在这里）
        if getattr(self, "_vt_enabled", False):
            # VT 模式：从后台 reader 线程的队列取字节，天然有界（queue 超时），
            # 不依赖 os.set_blocking/select。
            import queue

            try:
                if wait:
                    return self._stdin_q.get()  # 无限阻塞等输入
                return self._stdin_q.get_nowait() if not self._stdin_q.empty() else -1
            except (queue.Empty, AttributeError):
                return -1
        try:
            fd = self._stdin.fileno()
        except (AttributeError, OSError):
            return -1
        if not wait:
            try:
                import select

                r, _, _ = select.select([fd], [], [], 0.02)
                if not r:
                    return -1
            except Exception:
                pass
        try:
            data = self._stdin.buffer.read(1)
        except (AttributeError, OSError):
            data = b""
        if not data:
            return -1
        return data[0]

    # -- 解析 -----------------------------------------------------------

    def _read_vt_byte(self, timeout: float = 0.05) -> int:
        """有界读取 VT 序列续字节。

        msvcrt 路径绝不能直接 ``getwch()`` 阻塞——ConPTY 只送来半截序列时
        （如鼠标 SGR 的续字节延迟/丢失），getwch 会永久阻塞，key pump 线程
        挂死 → 整个输入（鼠标+键盘）冻结，表现成"又卡住了"。这里先轮询
        ``kbhit()``（带超时），有字节才 getwch（立即返回）；超时返回 -1。

        stdin 字节路径同理：set_blocking(False) + buffer.read 有界探测，
        避免半截序列把 key pump 卡死在阻塞读上。
        """
        import queue
        import time

        deadline = time.monotonic() + timeout
        if self._msvcrt:
            import msvcrt

            while not msvcrt.kbhit() and time.monotonic() < deadline:
                time.sleep(0.002)
            if not msvcrt.kbhit():
                return -1
            return self._read_byte(wait=True)
        if getattr(self, "_vt_enabled", False):
            # VT 模式：从 reader 线程队列有界取（queue.get(timeout)）
            try:
                return self._stdin_q.get(timeout=timeout)
            except (queue.Empty, AttributeError):
                return -1
        # stdin 路径（mintty/Unix）：select 有界探测后阻塞读
        try:
            fd = self._stdin.fileno()
        except (AttributeError, OSError):
            return -1
        try:
            import select

            r, _, _ = select.select([fd], [], [], timeout)
            if not r:
                return -1
        except Exception:
            pass
        return self._read_byte(wait=True)

    def _read_vt(self):
        """已读到 ESC,继续解析后续序列,返回归一化键元组。"""
        b = self._read_vt_byte()
        if b == -1:
            return ("escape",)
        if b not in (ord("["), ord("O")):
            return ("escape",)  # ESC 后跟普通字符
        # CSI/SS3：收集参数直到最终字节
        param = ""
        while True:
            b = self._read_vt_byte()
            if b == -1:
                return ("escape",)  # 序列不完整：放弃，不阻塞
            ch = chr(b)
            if 0x40 <= b <= 0x7E:  # 最终字节
                if ch == "~":
                    # 括号粘贴标记：\x1b[200~ 开始 / \x1b[201~ 结束
                    if param == "200":
                        return ("paste_start",)
                    if param == "201":
                        return ("paste_end",)
                    return (_CSI_MAP.get(param + "~", "escape"),)
                if param.startswith("<"):
                    ev = _parse_sgr_mouse(param, ch)
                    if ev is not None:
                        return ("mouse", ev)
                return (_CSI_MAP.get(ch, "escape"),)
            param += ch

    def read_key(self):
        """阻塞读取并返回一个归一化键。

        括号粘贴期间（\x1b[200~ ... \x1b[201~）换行是字面量（print "\n"）而非
        Enter——否则粘贴多行文本会把每行都当成一次提交（claude 的优雅粘贴）。
        """
        while True:
            key = self._read_key_once()
            if key == ("paste_start",):
                self._pasting = True
                continue
            if key == ("paste_end",):
                self._pasting = False
                continue
            return key

    def _read_key_once(self):
        b = self._read_byte(wait=True)
        if b == -1:
            return ("eof",)
        if self._msvcrt and b in (0xE0, 0x00):
            # legacy 扫描码：续字节也用有界读取，避免半截扫描码阻塞 key pump
            code = self._read_vt_byte()
            if code == -1:
                return ("unknown",)
            return (_SCAN_MAP.get(chr(code) if code >= 0 else "", "unknown"),)
        if b == 27:  # ESC / VT 序列起点
            if self._msvcrt:
                import msvcrt
                import time

                # 竞态防护：ConPTY 可能把 CSI 序列分片到达——ESC 先读出，续字节
                # `[<...;M` 稍后才进缓冲区。单次 kbhit() 判断会漏掉，把鼠标 SGR
                # /方向键序列拆成 ("escape",) + ("print","[") 乱码（滚轮丢事件、
                # 输入框进垃圾）。有界轮询等齐续字节；等不到才当独立 ESC。
                deadline = time.monotonic() + 0.04
                while not msvcrt.kbhit() and time.monotonic() < deadline:
                    time.sleep(0.002)
                if msvcrt.kbhit():
                    return self._read_vt()
                return ("escape",)
            return self._read_vt()
        if b in (13, 10):
            # 粘贴中的换行是字面量（进入输入框），不是提交键
            if self._pasting:
                return ("print", "\n")
            return ("enter",)
        if b == 9:
            return ("tab",)
        if b in (127, 8):
            return ("backspace",)
        if b == 3:
            return ("ctrl_c",)
        if b == 15:
            return ("ctrl_o",)
        if b == 4:
            return ("eof",)
        if b < 32:
            return ("ctrl", chr(b + 96))
        if b < 0x80:
            return ("print", chr(b))
        if self._msvcrt:
            # Windows 控制台 getwch 已返回完整宽字符码点（中文等 IME 确认字符）。
            # 不能当 UTF-8 首字节解析，否则中文会进 "unknown" 丢失。
            return ("print", chr(b))
        # UTF-8 多字节
        if b & 0xE0 == 0xC0:
            need = 1
        elif b & 0xF0 == 0xE0:
            need = 2
        elif b & 0xF8 == 0xF0:
            need = 3
        else:
            return ("unknown",)
        seq = bytearray([b])
        for _ in range(need):
            nb = self._read_byte(wait=True)
            if nb == -1:
                break
            seq.append(nb)
        try:
            return ("print", seq.decode("utf-8", errors="replace"))
        except Exception:
            return ("unknown",)
