"""跨平台剪贴板写入。优先 Windows 原生 API（不依赖第三方库），
其余平台退回 xclip。供 `/copy` 命令与 TUI 拖拽选区共用。"""

from __future__ import annotations

import sys


def set_clipboard(text: str) -> None:
    """把 `text` 写入系统剪贴板。任何失败都会抛出异常，由调用方处理。"""
    if sys.platform == "win32":
        _set_clipboard_windows(text)
    else:
        import subprocess

        subprocess.run(
            ["xclip", "-selection", "clipboard"],
            input=text,
            text=True,
            check=True,
        )


def _set_clipboard_windows(text: str) -> None:
    """通过 Windows 剪贴板 API（CF_UNICODETEXT）设置剪贴板文本。

    不依赖第三方库，也不受终端鼠标捕获影响。仅在 win32 上可用。
    """
    import ctypes
    from ctypes import wintypes  # noqa: F401  (确保 kernel32/user32 类型可用)

    CF_UNICODETEXT = 13
    data = text.encode("utf-16-le") + b"\x00\x00"
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # 64 位 Windows 上句柄/指针是 64 位，必须显式声明类型，否则会被截断成 32 位。
    c_void_p = ctypes.c_void_p
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL

    if not user32.OpenClipboard(None):
        raise RuntimeError("OpenClipboard failed")
    try:
        user32.EmptyClipboard()
        # GMEM_MOVEABLE | GMEM_ZEROINIT
        handle = kernel32.GlobalAlloc(0x0042, len(data))
        if not handle:
            raise RuntimeError("GlobalAlloc failed")
        try:
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                raise RuntimeError("GlobalLock failed")
            try:
                ctypes.memmove(ptr, data, len(data))
            finally:
                kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                raise RuntimeError("SetClipboardData failed")
        except Exception:
            kernel32.GlobalFree(handle)
            raise
    finally:
        user32.CloseClipboard()
