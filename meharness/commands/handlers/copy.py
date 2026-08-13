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
    if not history:
        ctx.ui.add_system_message("没有可复制的内容")
        return

    # 找最后一条 user 消息的索引，从它开始复制（你的问题 + 之后所有回复/工具过程）
    last_user_idx = -1
    for i, msg in enumerate(history):
        if getattr(msg, "role", "") == "user":
            last_user_idx = i
    start = last_user_idx if last_user_idx >= 0 else 0
    slice_msgs = history[start:]

    def _fmt(msg) -> str:
        role = "你" if getattr(msg, "role", "") == "user" else "AI"
        content = getattr(msg, "content", "")
        # content 可能是字符串或 block 列表（工具调用等）
        if isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, dict):
                    if b.get("type") == "tool_use":
                        parts.append(f"[工具调用 {b.get('name')}] {str(b.get('input'))[:200]}")
                    elif b.get("type") == "text":
                        parts.append(str(b.get("text")))
                    else:
                        parts.append(str(b)[:200])
                else:
                    parts.append(str(b)[:200])
            content = "\n".join(parts)
        return f"{role}: {content}"

    target = "\n\n".join(_fmt(m) for m in slice_msgs)
    if not target.strip():
        ctx.ui.add_system_message("没有可复制的内容")
        return

    try:
        _copy_text(target)
    except Exception as e:
        ctx.ui.add_system_message(f"复制失败: {e}")
        return

    preview = target if len(target) <= 120 else target[:120] + "…"
    ctx.ui.add_system_message(f"已复制最近一轮对话（{len(target)} 字符）：\n{preview}")


COPY_COMMAND = Command(
    name="copy",
    description="复制最后一条 AI 回复到剪贴板",
    usage="/copy",
    aliases=["cp"],
    type=CommandType.LOCAL_UI,
    handler=handle_copy,
)
