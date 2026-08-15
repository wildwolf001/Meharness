"""主 agent 规划能力回归：Task* 工具进主 agent 工具集 + 模糊任务会走 TaskCreate。

背景：系统提示词指示主 agent"用 TaskCreate 拆解工作"，但 Task* 此前只注册给
子 agent/队友，主 agent 调不到 → 模型想规划也无从下手。本测试验证：
1. register_main_agent_task_tools 把 4 个 Task* 工具注册进主 agent；
2. TeamManager 对 MAIN_AGENT_TEAM 懒创建本地任务板（无需建团队）；
3. 端到端：模糊任务 → mock 模型选择用 TaskCreate 规划 → 任务成功落库、
   对话有 tool_result、无错误；
4. TUI 对 TaskCreate 的 ToolUseEvent/ToolResultEvent 渲染进度行（⏳/✓）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meharness.agent import (
    Agent,
    StreamText,
    ToolResultEvent,
    ToolUseEvent,
)
from meharness.conversation import ConversationManager
from meharness.teams.manager import (
    MAIN_AGENT_TEAM,
    TeamManager,
    register_main_agent_task_tools,
)
from meharness.tools import create_default_registry
from meharness.tools.base import (
    StreamEnd,
    TextDelta,
    ToolCallComplete,
)


class _MockLLMClient:
    """返回预设脚本的 mock LLM（最小实现，不依赖 test_agent）。"""

    def __init__(self, responses: list[list]) -> None:
        self._responses = list(responses)
        self._call_index = 0

    async def stream(self, conversation, system="", tools=None):
        from typing import AsyncIterator
        from meharness.tools.base import StreamEvent
        _it: AsyncIterator[StreamEvent] = self._stream()
        async for e in _it:
            yield e

    async def _stream(self):
        if self._call_index >= len(self._responses):
            yield TextDelta("no more")
            yield StreamEnd(stop_reason="end_turn", input_tokens=1, output_tokens=1)
            return
        events = self._responses[self._call_index]
        self._call_index += 1
        for e in events:
            yield e


def _make_agent(tmp_path, monkeypatch, client):
    """构造带 Task* 工具、无权限检查的主 agent（Path.home 指向临时目录）。"""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    registry = create_default_registry()
    team_manager = TeamManager()
    register_main_agent_task_tools(registry, team_manager, agent_name="meharness")
    agent = Agent(client, registry, "openai-compat", work_dir=str(tmp_path))
    agent.team_name = MAIN_AGENT_TEAM
    agent._team_manager = team_manager
    return agent, team_manager


# ---------------------------------------------------------------------------
# 1. 注册 + 本地任务板
# ---------------------------------------------------------------------------

def test_register_main_agent_task_tools_registers_four(tmp_path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    registry = create_default_registry()
    register_main_agent_task_tools(registry, TeamManager())
    names = {t.name for t in registry.list_tools()}
    assert {"TaskCreate", "TaskList", "TaskGet", "TaskUpdate"} <= names


def test_get_task_store_lazily_creates_main_board(tmp_path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    tm = TeamManager()
    store = tm.get_task_store(MAIN_AGENT_TEAM)
    assert store is not None
    # 第二次取到同一实例
    assert tm.get_task_store(MAIN_AGENT_TEAM) is store
    # 落盘到 ~/.meharness/teams/main/tasks.json
    assert (fake_home / ".meharness" / "teams" / MAIN_AGENT_TEAM / "tasks.json").exists()


# ---------------------------------------------------------------------------
# 2. 端到端：模糊任务 → 模型用 TaskCreate 规划 → 成功落库
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_vague_task_planning_with_taskcreate(tmp_path, monkeypatch):
    """模糊任务下，模型（mock 选择规划）调用 TaskCreate：工具执行成功、
    任务写入本地任务板、conversation 得到 tool_result、无错误事件。"""
    client = _MockLLMClient([
        # 第 1 轮：模型面对模糊任务，先 TaskCreate 拆出子任务
        [
            TextDelta("Let me break this down."),
            ToolCallComplete("t1", "TaskCreate", {
                "title": "重构数据管道",
                "description": "拆出独立的 ETL 步骤",
            }),
            StreamEnd("end_turn", input_tokens=50, output_tokens=30),
        ],
        # 第 2 轮：继续规划
        [
            TextDelta("Also create a verification task."),
            ToolCallComplete("t2", "TaskCreate", {
                "title": "写验证测试",
            }),
            StreamEnd("end_turn", input_tokens=80, output_tokens=20),
        ],
        # 第 3 轮：规划完成，进入执行
        [
            TextDelta("Plan complete, now executing."),
            StreamEnd("end_turn", input_tokens=100, output_tokens=10),
        ],
    ])
    agent, team_manager = _make_agent(tmp_path, monkeypatch, client)

    conv = ConversationManager()
    # 模糊任务：不给步骤，看模型是否"主动规划"
    conv.add_user_message("这个项目的数据管道需要重构一下")

    events = []
    async for e in agent.run(conv):
        events.append(e)

    tool_uses = [e for e in events if isinstance(e, ToolUseEvent)]
    tool_results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert [e.tool_name for e in tool_uses].count("TaskCreate") == 2
    # 两次 TaskCreate 都成功（无 error）
    create_results = [r for r in tool_results if r.tool_name == "TaskCreate"]
    assert len(create_results) == 2
    assert all(not r.is_error for r in create_results)
    assert all("Task created" in r.output for r in create_results)

    # 任务真实落库到主 agent 本地任务板
    store = team_manager.get_task_store(MAIN_AGENT_TEAM)
    tasks = store.list_tasks()
    assert len(tasks) == 2
    assert {t.title for t in tasks} == {"重构数据管道", "写验证测试"}

    # conversation 里两条 tool_result 都承接上了，无悬空 tool_use、无错误
    all_results = [tr for m in conv.history for tr in m.tool_results]
    assert {tr.tool_use_id for tr in all_results} == {"t1", "t2"}
    assert all(not tr.is_error for tr in all_results)


# ---------------------------------------------------------------------------
# 3. TUI 进度展示：TaskCreate 的 ⏳ / ✓ 行
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_task_tool_renders_progress_in_tui():
    import re

    from meharness.repl.app import ReplApp

    def _plain() -> str:
        return re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(app.stream._screen_rows))

    app = ReplApp(providers=[])
    try:
        await app._handle_event(
            ToolUseEvent("TaskCreate", "t1", {"title": "拆解任务"}),
            accumulated="",
            history_cursor=0,
        )
        await app._handle_event(
            ToolResultEvent("t1", "TaskCreate", "Task created:\n ID: 1", False, 0.05),
            accumulated="",
            history_cursor=0,
        )
        rows = _plain()
        assert "Creating task" in rows  # tool_verb → "Creating task ..."
        assert "✓" in rows
    finally:
        app._stop_tool_spinner()
