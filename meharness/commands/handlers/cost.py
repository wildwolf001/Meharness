from __future__ import annotations

from meharness.commands.registry import Command, CommandContext, CommandType

# 每 1M token 的价格（(输入, 输出)），单位随你账号计费货币而定。
# deepseek 官方按 ¥/1M 计费；下面是占位值，请按你实际的 deepseek 账单校准，
# /cost 输出才会准。
PRICES_PER_1M: dict[str, tuple[float, float]] = {
    "deepseek-v4-flash": (0.27, 1.10),
    "deepseek-v4-pro": (1.00, 4.00),
}


def _price(model: str) -> tuple[float, float]:
    for prefix, price in PRICES_PER_1M.items():
        if model.startswith(prefix):
            return price
    return (0.0, 0.0)


async def handle_cost(ctx: CommandContext) -> None:
    input_tokens, output_tokens = ctx.ui.get_token_count()

    model = ""
    if ctx.agent is not None:
        client = getattr(ctx.agent, "client", None)
        model = getattr(client, "model", "") if client else ""

    price_in, price_out = _price(model or "deepseek-v4-flash")
    cost_in = input_tokens / 1_000_000 * price_in
    cost_out = output_tokens / 1_000_000 * price_out

    # 上下文水位：长期高效运转的关键提示（真实用量锚点 + 尾部估算）
    window = getattr(ctx.agent, "context_window", 0) or 0
    used = 0
    try:
        used = ctx.conversation.current_tokens()
    except Exception:
        pass
    ctx_pct = int(used / window * 100) if window else 0
    ctx_line = (
        f"上下文: {used:,} / {window:,} tokens（{ctx_pct}%）"
        if window else "上下文: 未知窗口"
    )

    lines = [
        "Token 用量与成本估算",
        "────────────────────",
        f"模型: {model or 'unknown'}",
        f"输入: {input_tokens:,} tokens",
        f"输出: {output_tokens:,} tokens",
        ctx_line,
        (
            f"估算成本: {cost_in + cost_out:.6f} "
            f"（输入 {cost_in:.6f} + 输出 {cost_out:.6f}）"
        ),
    ]
    ctx.ui.add_system_message("\n".join(lines))


COST_COMMAND = Command(
    name="cost",
    description="显示 token 用量与成本估算（/cost）",
    usage="/cost",
    type=CommandType.LOCAL,
    handler=handle_cost,
)
