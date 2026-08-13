# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com

from __future__ import annotations

import sys

from meharness.commands.registry import Command, CommandContext, CommandType


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


def _copy_text(text: str) -> None:
    if sys.platform == "win32":
        _set_clipboard_windows(text)
    else:
        import subprocess
        proc = subprocess.run(
            ["xclip", "-selection", "clipboard"],
            input=text, text=True, check=True,
        )


async def handle_copy(ctx: CommandContext) -> None:
    """把最后一条 assistant 回复复制到系统剪贴板。

    复制的是完整回复文本；若最后一条是空的（如纯工具调用），向上回退。
    """
    history = ctx.conversation.history if ctx.conversation else []
    target = ""
    for msg in reversed(history):
        if getattr(msg, "role", "") == "assistant" and getattr(msg, "content", ""):
            target = msg.content
            break

    if not target:
        ctx.ui.add_system_message("没有可复制的内容（还没有 assistant 回复）")
        return

    try:
        _copy_text(target)
    except Exception as e:
        ctx.ui.add_system_message(f"复制失败: {e}")
        return

    preview = target if len(target) <= 120 else target[:120] + "…"
    ctx.ui.add_system_message(f"已复制最后一条回复（{len(target)} 字符）：\n{preview}")


COPY_COMMAND = Command(
    name="copy",
    description="复制最后一条 AI 回复到剪贴板",
    usage="/copy",
    aliases=["cp"],
    type=CommandType.LOCAL_UI,
    handler=handle_copy,
)
