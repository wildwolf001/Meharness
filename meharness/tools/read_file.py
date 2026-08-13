# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from meharness.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from meharness.cache import FileCache
    from meharness.tools.file_state_cache import FileStateCache


class Params(BaseModel):
    file_path: str = Field(description="Absolute or relative path to the file to read")
    offset: int = Field(default=0, description="Line offset to start reading from (0-based)")
    limit: int = Field(default=2000, description="Maximum number of lines to read")


class ReadFile(Tool):
    name = "ReadFile"
    description = (
        "Reads a file from the local filesystem. You can access any file directly by using this tool.\n"
        "Assume this tool is able to read all files on the machine. If the User provides a path to a file assume that "
        "path is valid. It is okay to read a file that does not exist; an error will be returned.\n"
        "\n"
        "Usage:\n"
        "- The file_path parameter must be an absolute path, not a relative path\n"
        "- By default, it reads up to 2000 lines starting from the beginning of the file\n"
        "- You can optionally specify a line offset and limit (especially handy for long files), but it's recommended "
        "to read the whole file by not providing these parameters\n"
        "- Results are returned using cat -n format, with line numbers starting at 1\n"
        "- This tool can only read files, not directories. To read a directory, use an ls command via the Bash tool.\n"
        "- If you read a file that exists but has empty contents you will receive a system reminder warning in place of file contents."
    )
    params_model = Params
    category = "read"
    is_concurrency_safe = True


    def __init__(self, file_cache: FileCache | None = None, file_state_cache: FileStateCache | None = None) -> None:
        self._cache = file_cache
        self._state_cache = file_state_cache


    async def execute(self, params: Params) -> ToolResult:
        path = Path(params.file_path)
        if not path.exists():
            return ToolResult(output=f"Error: file not found: {params.file_path}", is_error=True)
        if not path.is_file():
            return ToolResult(output=f"Error: not a file: {params.file_path}", is_error=True)

        resolved = str(path.resolve())

        try:
            text = self._cache.get(resolved) if self._cache else None
            if text is None:
                text = path.read_text(encoding="utf-8")
                if self._cache:
                    self._cache.put(resolved, text)
        except Exception as e:
            return ToolResult(output=f"Error reading file: {e}", is_error=True)

        if self._state_cache:
            try:
                mtime_ns = path.stat().st_mtime_ns
                self._state_cache.record(resolved, text, mtime_ns)
            except OSError:
                pass

        lines = text.splitlines()
        selected = lines[params.offset : params.offset + params.limit]
        numbered = [f"{i + params.offset + 1}\t{line}" for i, line in enumerate(selected)]
        return ToolResult(output="\n".join(numbered))
