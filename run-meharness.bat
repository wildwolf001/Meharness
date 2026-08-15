@echo off
REM meharness launcher (Windows). Run from any terminal or double-click.
REM Usage: run-meharness.bat [--ui textual] [-p "prompt"]
cd /d %~dp0
set MEHARNESS_COORDINATOR_MODE=1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
".venv\Scripts\python" -m meharness %*
