# WebSearch 工具：供文献检索等需要联网查资料的专家使用。

from __future__ import annotations

import html
import re

import httpx
from pydantic import BaseModel, Field

from meharness.tools.base import Tool, ToolResult


class Params(BaseModel):
    query: str = Field(description="搜索查询词（英文效果更稳）")
    max_results: int = Field(default=5, ge=1, le=10, description="返回结果条数")


class WebSearch(Tool):
    name = "WebSearch"
    description = (
        "Search the web for a query and return ranked results with title, URL and snippet. "
        "Use for finding papers, documentation, news, or current information."
    )
    params_model = Params
    category = "read"
    is_concurrency_safe = True

    async def execute(self, params: Params) -> ToolResult:
        try:
            results = await self._search(params.query, params.max_results)
        except Exception as e:
            return ToolResult(output=f"WebSearch error: {e}", is_error=True)

        if not results:
            return ToolResult(output="No results found.")
        lines = [f"搜索结果（{len(results)} 条）:"]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']}")
            lines.append(f"   {r['url']}")
            snippet = r.get("snippet", "")
            if snippet:
                lines.append(f"   {snippet}")
        return ToolResult(output="\n".join(lines))

    async def _search(self, query: str, max_results: int) -> list[dict]:
        """默认用 DuckDuckGo HTML 端点（无需 key）。"""
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": "Mozilla/5.0 meharness"},
            )
            resp.raise_for_status()

        results: list[dict] = []
        # result__a 是链接（含标题），result__snippet 是摘要
        links = re.findall(
            r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            resp.text,
            flags=re.DOTALL,
        )
        snippets = re.findall(
            r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
            resp.text,
            flags=re.DOTALL,
        )
        for i, (href, title) in enumerate(links[:max_results]):
            url = html.unescape(href)
            # DuckDuckGo 重定向链接：去前缀 → 百分号解码 → 去追踪参数
            url = re.sub(r"^//duckduckgo\.com/l/\?uddg=", "", url)
            import urllib.parse
            url = urllib.parse.unquote(url)
            url = url.split("&rut=")[0]
            title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
            snippet = ""
            if i < len(snippets):
                snippet = html.unescape(re.sub(r"<[^>]+>", "", snippets[i])).strip()
            results.append({"title": title, "url": url, "snippet": snippet})
        return results
