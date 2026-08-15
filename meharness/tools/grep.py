from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from meharness.tools.base import SKIP_DIRS, Tool, ToolResult


class Params(BaseModel):
    pattern: str = Field(description="Regex pattern to search for")
    path: str = Field(default=".", description="Base directory to search from")
    include: str = Field(default="", description="Glob filter for filenames (e.g. '*.py')")


class Grep(Tool):
    name = "Grep"
    description = (
        "A powerful search tool for file contents\n"
        "\n"
        "Usage:\n"
        "- ALWAYS use Grep for search tasks. NEVER invoke `grep` or `rg` as a Bash command. The Grep tool has been "
        "optimized for correct permissions and access.\n"
        "- Supports full regex syntax (e.g., \"log.*Error\", \"function\\s+\\w+\")\n"
        "- Filter files with the include parameter, a glob filter on filenames (e.g., \"*.py\", \"**/*.tsx\")\n"
        "- Returns matches as `file:line:content` lines\n"
        "- Use Agent tool for open-ended searches requiring multiple rounds\n"
        "- Pattern syntax: Python regex - literal braces need escaping (use `interface\\{\\}` to find `interface{}` in "
        "Go code)\n"
        "- Matching is checked per line; patterns match within single lines only"
    )
    params_model = Params
    category = "read"
    is_concurrency_safe = True


    async def execute(self, params: Params) -> ToolResult:
        base = Path(params.path)
        if not base.exists():
            return ToolResult(output=f"Error: path not found: {params.path}", is_error=True)

        try:
            regex = re.compile(params.pattern)
        except re.error as e:
            return ToolResult(output=f"Error: invalid regex: {e}", is_error=True)

        glob_pattern = params.include if params.include else "**/*"
        if not glob_pattern.startswith("**/"):
            glob_pattern = "**/" + glob_pattern

        results: list[str] = []
        for file_path in sorted(base.glob(glob_pattern)):
            if not file_path.is_file():
                continue
            if any(part in SKIP_DIRS for part in file_path.parts):
                continue
            try:
                text = file_path.read_text(encoding="utf-8", errors="ignore")
            except (OSError, UnicodeDecodeError):
                continue
            for line_num, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    rel = file_path.relative_to(base)
                    results.append(f"{rel}:{line_num}:{line}")

        if not results:
            return ToolResult(output="No matches found.")
        return ToolResult(output="\n".join(results))

