# WebFetch 工具：抓取 URL 并提取可读文本，供文献检索阅读论文/页面。

from __future__ import annotations

import html
import re

import httpx
from pydantic import BaseModel, Field

from meharness.tools.base import Tool, ToolResult


class Params(BaseModel):
    url: str = Field(description="要抓取的 URL")
    max_chars: int = Field(default=8000, ge=200, le=40000, description="返回文本上限字符")


class WebFetch(Tool):
    name = "WebFetch"
    description = (
        "Fetch a URL and extract readable text (strips HTML). "
        "Use to read a paper page, doc, or article found by WebSearch."
    )
    params_model = Params
    category = "read"
    is_concurrency_safe = True

    async def execute(self, params: Params) -> ToolResult:
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                resp = await client.get(
                    params.url,
                    headers={"User-Agent": "Mozilla/5.0 meharness"},
                )
                resp.raise_for_status()
        except Exception as e:
            return ToolResult(output=f"WebFetch error: {e}", is_error=True)

        text = self._extract_text(resp.text)
        if not text:
            return ToolResult(output="无法从该页面提取文本（可能是 JS 渲染页面）。")
        if len(text) > params.max_chars:
            text = text[: params.max_chars] + "\n… (截断)"
        return ToolResult(output=f"URL: {params.url}\n\n{text}")

    @staticmethod
    def _extract_text(raw: str) -> str:
        # 去掉脚本/样式，其余按块转文本
        raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.DOTALL | re.I)
        # 标题/段落换行
        raw = re.sub(r"</(p|div|h[1-6]|li|br|tr|section|article)>", "\n", raw, flags=re.I)
        raw = re.sub(r"<[^>]+>", " ", raw)
        raw = html.unescape(raw)
        lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)
