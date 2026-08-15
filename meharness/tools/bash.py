from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from meharness.tools.base import Tool, ToolResult

MAX_TIMEOUT = 600


class Params(BaseModel):
    command: str = Field(description="Shell command to execute")
    timeout: int = Field(default=120, description="Timeout in seconds (max 600)")


class Bash(Tool):
    name = "Bash"
    description = (
        "Executes a given bash command and returns its output.\n"
        "\n"
        "The working directory persists between commands, but shell state does not. "
        "The shell environment is initialized from the user's profile (bash or zsh).\n"
        "\n"
        "IMPORTANT: Avoid using this tool to run `find`, `grep`, `cat`, `head`, `tail`, `sed`, `awk`, or `echo` "
        "commands, unless explicitly instructed or after you have verified that a dedicated tool cannot accomplish "
        "your task. Instead, use the appropriate dedicated tool as this will provide a much better experience for the user:\n"
        "- File search: Use Glob (NOT find or ls)\n"
        "- Content search: Use Grep (NOT grep or rg)\n"
        "- Read files: Use ReadFile (NOT cat/head/tail)\n"
        "- Edit files: Use EditFile (NOT sed/awk)\n"
        "- Write files: Use WriteFile (NOT echo >/cat <<EOF)\n"
        "- Communication: Output text directly (NOT echo/printf)\n"
        "\n"
        "While the Bash tool can do similar things, it's better to use the built-in tools as they provide a better "
        "user experience and make it easier to review tool calls and give permission.\n"
        "\n"
        "# Instructions\n"
        "- If your command will create new directories or files, first use this tool to run `ls` to verify the parent "
        "directory exists and is the correct location.\n"
        "- Always quote file paths that contain spaces with double quotes in your command (e.g., cd \"path with spaces/file.txt\")\n"
        "- Try to maintain your current working directory throughout the session by using absolute paths and avoiding "
        "usage of `cd`. You may use `cd` if the User explicitly requests it.\n"
        "- You may specify an optional timeout in seconds (up to 600s / 10 minutes). By default, your command will "
        "timeout after 120s (2 minutes).\n"
        "- When issuing multiple commands:\n"
        "  - If the commands are independent and can run in parallel, make multiple Bash tool calls in a single message. "
        "Example: if you need to run \"git status\" and \"git diff\", send a single message with two Bash tool calls in parallel.\n"
        "  - If the commands depend on each other and must run sequentially, use a single Bash call with '&&' to chain them together.\n"
        "  - Use ';' only when you need to run commands sequentially but don't care if earlier commands fail.\n"
        "  - DO NOT use newlines to separate commands (newlines are ok in quoted strings).\n"
        "- For git commands:\n"
        "  - Prefer to create a new commit rather than amending an existing commit.\n"
        "  - Before running destructive operations (e.g., git reset --hard, git push --force, git checkout --), "
        "consider whether there is a safer alternative that achieves the same goal. Only use destructive operations "
        "when they are truly the best approach.\n"
        "  - Never skip hooks (--no-verify) or bypass signing (--no-gpg-sign, -c commit.gpgsign=false) unless the user "
        "has explicitly asked for it. If a hook fails, investigate and fix the underlying issue.\n"
        "- Avoid unnecessary `sleep` commands:\n"
        "  - Do not sleep between commands that can run immediately — just run them.\n"
        "  - Do not retry failing commands in a sleep loop — diagnose the root cause.\n"
        "  - If you must poll an external process, use a check command (e.g. `gh run view`) rather than sleeping first.\n"
        "  - If you must sleep, keep the duration short (1-5 seconds) to avoid blocking the user."
    )
    params_model = Params
    category = "command"


    async def execute(self, params: Params) -> ToolResult:
        import os
        import shutil
        import sys
        from pathlib import Path

        timeout = min(params.timeout, MAX_TIMEOUT)

        # Windows 上 `asyncio.create_subprocess_shell` 默认用 cmd.exe，而 agent 按
        # bash 语法写命令（ls/find/管道/环境变量），cmd 必然失败。有 git bash 时
        # 用 `bash -c` 执行，保证 Unix 命令可用；否则退回 cmd。
        #
        # 检测顺序：PATH 里的 bash → 由 `git` 安装位置反推（非标准安装如
        # D:\git\Git，PATH 里只有 cmd 目录，没有 bash.exe）→ 常见默认路径。
        executable = None
        if sys.platform == "win32":
            bash = shutil.which("bash")
            if bash is None:
                git = shutil.which("git")
                if git:
                    # git 在 ...\Git\cmd\git.exe → 安装根 = parents[1]
                    git_root = Path(git).resolve().parents[1]
                    for cand in (
                        git_root / "bin" / "bash.exe",
                        git_root / "usr" / "bin" / "bash.exe",
                    ):
                        if cand.exists():
                            bash = str(cand)
                            break
            if bash is None:
                for cand in (
                    r"C:\Program Files\Git\bin\bash.exe",
                    r"C:\Program Files\Git\usr\bin\bash.exe",
                ):
                    if os.path.exists(cand):
                        bash = cand
                        break
            if bash:
                executable = bash

        try:
            if executable:
                proc = await asyncio.create_subprocess_exec(
                    executable, "-c", params.command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    params.command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult(output=f"Error: command timed out after {timeout}s", is_error=True)
        except Exception as e:
            return ToolResult(output=f"Error executing command: {e}", is_error=True)

        parts: list[str] = []
        if stdout:
            parts.append(f"STDOUT:\n{stdout.decode(errors='replace')}")
        if stderr:
            parts.append(f"STDERR:\n{stderr.decode(errors='replace')}")
        if not parts:
            parts.append("(no output)")

        output = "\n".join(parts)
        return ToolResult(output=output, is_error=proc.returncode != 0)

