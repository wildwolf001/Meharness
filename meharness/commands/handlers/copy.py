# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com

from __future__ import annotations

from meharness.commands.registry import Command, CommandContext, CommandType
from meharness.ui.clipboard import set_clipboard as _copy_text


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
