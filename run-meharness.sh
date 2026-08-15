#!/usr/bin/env bash
# meharness launcher (git bash / WSL). Usage: ./run-meharness.sh [--ui textual] [-p "prompt"]
cd "$(dirname "$0")"
export MEHARNESS_COORDINATOR_MODE=1
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
exec ".venv/Scripts/python" -m meharness "$@"
