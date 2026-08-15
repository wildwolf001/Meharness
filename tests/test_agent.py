# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com

"""Agent Loop 的集成测试 —— 以编程方式逐项验证 checklist。"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest

from meharness.agent import (
    Agent,
    ErrorEvent,
    LoopComplete,
    PermissionRequest,
    PermissionResponse,
    StreamText,
    ToolResultEvent,
    ToolUseEvent,
    TurnComplete,
    UsageEvent,
    partition_tool_calls,
)
from meharness.prompts import build_environment_context, build_plan_mode_reminder, build_system_prompt
from meharness.client import LLMClient
from meharness.conversation import ConversationManager
from meharness.serialization import build_anthropic_messages
from meharness.tools import create_default_registry
from meharness.tools.base import (
    StreamEnd,
    StreamEvent,
    TextDelta,
    ToolCallComplete,
)

# ---------------------------------------------------------------------------
# 返回预设脚本响应的 mock LLM 客户端
# ---------------------------------------------------------------------------

class MockLLMClient(LLMClient):
    def __init__(self, responses: list[list[StreamEvent]], yield_control: bool = False) -> None:
        self._responses = list(responses)
        self._call_index = 0
        self._yield_control = yield_control

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if self._call_index >= len(self._responses):
            yield TextDelta(text="No more responses")
            yield StreamEnd(stop_reason="end_turn", input_tokens=1, output_tokens=1)
            return
        events = self._responses[self._call_index]
        self._call_index += 1
        for e in events:
            if self._yield_control:
                await asyncio.sleep(0)
            yield e

def _collect(events: list) -> dict[str, list]:
    result: dict[str, list] = {
        "text": [], "tool_use": [], "tool_result": [],
        "turn": [], "loop": [], "usage": [], "error": [],
    }
    for e in events:
        if isinstance(e, StreamText):
            result["text"].append(e.text)
        elif isinstance(e, ToolUseEvent):
            result["tool_use"].append(e)
        elif isinstance(e, ToolResultEvent):
            result["tool_result"].append(e)
        elif isinstance(e, TurnComplete):
            result["turn"].append(e)
        elif isinstance(e, LoopComplete):
            result["loop"].append(e)
        elif isinstance(e, UsageEvent):
            result["usage"].append(e)
        elif isinstance(e, ErrorEvent):
            result["error"].append(e)
    return result

# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_step_tool_call():
    """Agent 调用一次 ReadFile，拿到结果后停止。"""
    client = MockLLMClient([
        # 第 1 轮：模型调用 ReadFile
        [
            TextDelta("Let me read the file."),
            ToolCallComplete("t1", "ReadFile", {"file_path": "README.md"}),
            StreamEnd("end_turn", input_tokens=10, output_tokens=20),
        ],
        # 第 2 轮：模型给出最终答案
        [
            TextDelta("The file contains project info."),
            StreamEnd("end_turn", input_tokens=30, output_tokens=15),
        ],
    ])
    registry = create_default_registry()
    agent = Agent(client, registry, "anthropic", work_dir=".")
    conv = ConversationManager()
    conv.add_user_message("Read README.md")

    events = []
    async for e in agent.run(conv):
        events.append(e)

    c = _collect(events)
    assert len(c["tool_use"]) == 1
    assert c["tool_use"][0].tool_name == "ReadFile"
    assert len(c["tool_result"]) == 1
    assert len(c["turn"]) == 1
    assert len(c["loop"]) == 1
    assert c["loop"][0].total_turns == 2

@pytest.mark.asyncio
async def test_multi_step_autonomous():
    """Agent 先 WriteFile 再 ReadFile 然后停止 —— 端到端的多步流程。"""
    client = MockLLMClient([
        # 第 1 轮：WriteFile
        [
            TextDelta("Creating file."),
            ToolCallComplete("t1", "WriteFile", {"file_path": "/tmp/meharness_test_hello.txt", "content": "Hello World"}),
            StreamEnd("end_turn", input_tokens=10, output_tokens=20),
        ],
        # 第 2 轮：ReadFile 进行验证
        [
            TextDelta("Verifying content."),
            ToolCallComplete("t2", "ReadFile", {"file_path": "/tmp/meharness_test_hello.txt"}),
            StreamEnd("end_turn", input_tokens=40, output_tokens=25),
        ],
        # 第 3 轮：最终答案
        [
            TextDelta("File created and verified. Content is correct."),
            StreamEnd("end_turn", input_tokens=60, output_tokens=30),
        ],
    ])
    registry = create_default_registry()
    agent = Agent(client, registry, "anthropic", work_dir="/tmp")
    conv = ConversationManager()
    conv.add_user_message("Create hello.txt with Hello World, then verify")

    # 幂等：上一轮运行可能已创建该文件，先删掉，否则 WriteFile 会因
    # read-before-write 状态缓存（文件已存在但本会话未读过）而报错。
    import os
    target = "/tmp/meharness_test_hello.txt"
    try:
        os.unlink(target)
    except FileNotFoundError:
        pass

    events = []
    async for e in agent.run(conv):
        events.append(e)

    c = _collect(events)
    assert len(c["tool_use"]) == 2
    assert c["tool_use"][0].tool_name == "WriteFile"
    assert c["tool_use"][1].tool_name == "ReadFile"
    assert len(c["turn"]) == 2
    assert len(c["loop"]) == 1
    assert c["loop"][0].total_turns == 3
    # 验证文件确实被创建了
    assert not c["tool_result"][0].is_error
    assert not c["tool_result"][1].is_error

class _RateLimitOnceClient(LLMClient):
    """第一次调用抛 RateLimitError，之后正常——验证指数退避重试。"""

    def __init__(self, ok_responses: list[list[StreamEvent]]) -> None:
        self._ok = ok_responses
        self._calls = 0

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self._calls += 1
        if self._calls == 1:
            from meharness.client import RateLimitError

            raise RateLimitError("rate limited", retry_after=0.01)
        for e in self._ok:
            yield e


@pytest.mark.asyncio
async def test_transient_error_retried():
    """限流/网络瞬时错误应指数退避重试，而不是直接终止回合。"""
    client = _RateLimitOnceClient([
        TextDelta("Recovered after rate limit."),
        StreamEnd("end_turn", input_tokens=10, output_tokens=5),
    ])
    registry = create_default_registry()
    agent = Agent(client, registry, "anthropic")
    conv = ConversationManager()
    conv.add_user_message("Do something")

    events = []
    async for e in agent.run(conv):
        events.append(e)

    c = _collect(events)
    assert client._calls == 2  # 第一次失败 + 重试成功
    assert len(c["loop"]) == 1  # 回合正常完成
    assert "Recovered after rate limit" in "".join(c["text"])
    assert len(c["error"]) == 0


@pytest.mark.asyncio
async def test_empty_response_recovered():
    """空响应（无文本无工具）应触发恢复重试，而不是静默停止。"""
    client = MockLLMClient([
        [StreamEnd("end_turn", input_tokens=1, output_tokens=1)],  # 空响应
        [
            TextDelta("Proceeding now."),
            StreamEnd("end_turn", input_tokens=2, output_tokens=2),
        ],
    ])
    registry = create_default_registry()
    agent = Agent(client, registry, "anthropic")
    conv = ConversationManager()
    conv.add_user_message("Do something")

    events = []
    async for e in agent.run(conv):
        events.append(e)

    c = _collect(events)
    assert len(c["loop"]) == 1  # 没有停在空响应上
    assert "Proceeding now" in "".join(c["text"])


@pytest.mark.asyncio
async def test_stop_end_turn():
    """模型以 end_turn 自然停止。"""
    client = MockLLMClient([
        [
            TextDelta("Hello! How can I help?"),
            StreamEnd("end_turn", input_tokens=5, output_tokens=10),
        ],
    ])
    registry = create_default_registry()
    agent = Agent(client, registry, "anthropic")
    conv = ConversationManager()
    conv.add_user_message("Hi")

    events = []
    async for e in agent.run(conv):
        events.append(e)

    c = _collect(events)
    assert len(c["loop"]) == 1
    assert c["loop"][0].total_turns == 1
    assert len(c["error"]) == 0

@pytest.mark.asyncio
async def test_stop_max_iterations():
    """Agent 在达到 max_iterations 后停止。"""
    # 每个响应都带有工具调用，因此循环永远不会自然结束
    responses = []
    for i in range(5):
        responses.append([
            TextDelta(f"Step {i}"),
            ToolCallComplete(f"t{i}", "ReadFile", {"file_path": "README.md"}),
            StreamEnd("end_turn", input_tokens=10, output_tokens=10),
        ])

    client = MockLLMClient(responses)
    registry = create_default_registry()
    agent = Agent(client, registry, "anthropic", max_iterations=2)
    conv = ConversationManager()
    conv.add_user_message("Do something")

    events = []
    async for e in agent.run(conv):
        events.append(e)

    c = _collect(events)
    assert len(c["error"]) == 1
    assert "maximum iterations" in c["error"][0].message

@pytest.mark.asyncio
async def test_stop_cancel():
    """Agent 在收到 CancelledError 时干净地停止。"""

    class SlowMockClient(LLMClient):
        """在事件之间 sleep 的 mock 客户端，以便留出取消的时机。"""
        def __init__(self) -> None:
            self._call_count = 0

        async def stream(
            self,
            conversation: ConversationManager,
            system: str = "",
            tools: list[dict[str, Any]] | None = None,
        ) -> AsyncIterator[StreamEvent]:
            self._call_count += 1
            await asyncio.sleep(0.01)
            yield TextDelta(f"Step {self._call_count}")
            await asyncio.sleep(0.01)
            yield ToolCallComplete(f"t{self._call_count}", "ReadFile", {"file_path": "README.md"})
            await asyncio.sleep(0.01)
            yield StreamEnd("end_turn", input_tokens=10, output_tokens=10)

    client = SlowMockClient()
    registry = create_default_registry()
    agent = Agent(client, registry, "anthropic")
    conv = ConversationManager()
    conv.add_user_message("Do something")

    events: list = []
    cancelled = False

    async def run_agent():
        async for e in agent.run(conv):
            events.append(e)

    task = asyncio.create_task(run_agent())
    await asyncio.sleep(0.15)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        cancelled = True

    assert cancelled
    c = _collect(events)
    assert len(c["turn"]) >= 1
    assert len(c["turn"]) < 50

@pytest.mark.asyncio
async def test_stop_consecutive_unknown_tools():
    """Agent 在连续 3 次调用未知工具后停止。"""
    responses = []
    for i in range(5):
        responses.append([
            TextDelta(f"Trying tool {i}"),
            ToolCallComplete(f"t{i}", "NonExistentTool", {"arg": "val"}),
            StreamEnd("end_turn", input_tokens=10, output_tokens=10),
        ])

    client = MockLLMClient(responses)
    registry = create_default_registry()
    agent = Agent(client, registry, "anthropic")
    conv = ConversationManager()
    conv.add_user_message("Do something")

    events = []
    async for e in agent.run(conv):
        events.append(e)

    c = _collect(events)
    assert len(c["error"]) == 1
    assert "unknown tool" in c["error"][0].message

@pytest.mark.asyncio
async def test_message_splicing():
    """assistant 消息包含 text + 多个 tool_use；对应的 tool_result 被打包在一起。"""
    client = MockLLMClient([
        # 第 1 轮：一个响应里包含两次工具调用
        [
            TextDelta("Reading two files."),
            ToolCallComplete("t1", "ReadFile", {"file_path": "README.md"}),
            ToolCallComplete("t2", "ReadFile", {"file_path": "pyproject.toml"}),
            StreamEnd("end_turn", input_tokens=10, output_tokens=20),
        ],
        # 第 2 轮：最终响应
        [
            TextDelta("Done."),
            StreamEnd("end_turn", input_tokens=30, output_tokens=10),
        ],
    ])
    registry = create_default_registry()
    agent = Agent(client, registry, "anthropic", work_dir=".")
    conv = ConversationManager()
    conv.add_user_message("Read both files")

    events = []
    async for e in agent.run(conv):
        events.append(e)

    # 检查对话历史
    msgs = build_anthropic_messages(conv.get_messages())
    # build_anthropic_messages 会把相邻的 user 纯文本消息合并（env_context + 用户提问
    # 合成一条）。所以是 4 条：user(合并) + assistant(text+2 tool_use) + user(2 tool_result) + assistant(最终)
    assert len(msgs) == 4
    assert msgs[0]["role"] == "user"
    assert "Read both files" in msgs[0]["content"]  # 合并后用户提问仍在
    assistant_msg = msgs[1]
    assert assistant_msg["role"] == "assistant"
    assert len(assistant_msg["content"]) == 3  # text + 2 个 tool_use
    tool_results_msg = msgs[2]
    assert tool_results_msg["role"] == "user"
    assert len(tool_results_msg["content"]) == 2  # 2 个 tool_result
    assert tool_results_msg["content"][0]["tool_use_id"] == "t1"
    assert tool_results_msg["content"][1]["tool_use_id"] == "t2"

@pytest.mark.asyncio
async def test_askuser_question_yields_event_not_crash():
    """AskUserQuestion 必须 yield AskUserEvent 交给 app 挂弹窗，而非被当
    (result, elapsed, is_unknown) 元组解包。回归保护曾出现的
    "cannot unpack non-iterable AskUserEvent object" 崩溃。
    """
    from meharness.tools.ask_user import AskUserEvent, AskUserTool

    client = MockLLMClient([
        [
            TextDelta("I need to ask."),
            ToolCallComplete(
                "t1",
                "AskUserQuestion",
                {"questions": [
                    {"type": "radio", "name": "choice",
                     "message": "选哪个?", "options": ["A", "B"]}
                ]},
            ),
            StreamEnd("end_turn", input_tokens=10, output_tokens=20),
        ],
        [TextDelta("Done."), StreamEnd("end_turn", input_tokens=30, output_tokens=10)],
    ])
    registry = create_default_registry()
    registry.register(AskUserTool())
    agent = Agent(client, registry, "anthropic", work_dir=".")
    conv = ConversationManager()
    conv.add_user_message("Ask me")

    seen_events = []
    tool_results = []
    async for e in agent.run(conv):
        if isinstance(e, AskUserEvent):
            seen_events.append(e)
            e.future.set_result({"choice": "A"})  # app 挂弹窗后回填答案
        elif isinstance(e, ToolResultEvent):
            tool_results.append(e)

    # 事件被正确向上 yield，且整个 run 跑完（此前这里会抛 TypeError）
    assert len(seen_events) == 1
    assert any("choice: A" in tr.output for tr in tool_results)

@pytest.mark.asyncio
async def test_concurrent_batch_execution():
    """多个 ReadFile 调用并发执行（属于同一批次）。"""
    client = MockLLMClient([
        [
            ToolCallComplete("t1", "ReadFile", {"file_path": "README.md"}),
            ToolCallComplete("t2", "ReadFile", {"file_path": "pyproject.toml"}),
            StreamEnd("end_turn", input_tokens=10, output_tokens=20),
        ],
        [
            TextDelta("Both files read."),
            StreamEnd("end_turn", input_tokens=30, output_tokens=10),
        ],
    ])
    registry = create_default_registry()
    agent = Agent(client, registry, "anthropic", work_dir=".")
    conv = ConversationManager()
    conv.add_user_message("Read both")

    events = []
    async for e in agent.run(conv):
        events.append(e)

    c = _collect(events)
    assert len(c["tool_result"]) == 2
    # 两个都应成功（这些文件在项目根目录下存在）
    assert all(not r.is_error for r in c["tool_result"])

@pytest.mark.asyncio
async def test_token_usage_accumulates():
    """Usage 事件展示的是累计的 token 数量。"""
    client = MockLLMClient([
        [
            TextDelta("Step 1"),
            ToolCallComplete("t1", "ReadFile", {"file_path": "README.md"}),
            StreamEnd("end_turn", input_tokens=100, output_tokens=50),
        ],
        [
            TextDelta("Step 2"),
            ToolCallComplete("t2", "ReadFile", {"file_path": "README.md"}),
            StreamEnd("end_turn", input_tokens=200, output_tokens=80),
        ],
        [
            TextDelta("Done."),
            StreamEnd("end_turn", input_tokens=300, output_tokens=100),
        ],
    ])
    registry = create_default_registry()
    agent = Agent(client, registry, "anthropic", work_dir=".")
    conv = ConversationManager()
    conv.add_user_message("Test")

    events = []
    async for e in agent.run(conv):
        events.append(e)

    c = _collect(events)
    assert len(c["usage"]) == 3
    assert c["usage"][0].input_tokens == 100
    assert c["usage"][0].output_tokens == 50
    assert c["usage"][1].input_tokens == 300
    assert c["usage"][1].output_tokens == 130
    assert c["usage"][2].input_tokens == 600
    assert c["usage"][2].output_tokens == 230

@pytest.mark.asyncio
async def test_plan_mode():
    """通过 permission_mode 切换 plan 模式。"""
    from meharness.permissions import PermissionMode

    registry = create_default_registry()
    agent = Agent(MockLLMClient([]), registry, "anthropic")

    agent.set_permission_mode(PermissionMode.PLAN)
    assert agent.plan_mode is True

    agent.set_permission_mode(PermissionMode.DEFAULT)
    assert agent.plan_mode is False
    schemas = registry.get_all_schemas()
    names = [s["name"] for s in schemas]
    assert "WriteFile" in names
    assert "EditFile" in names
    assert "Bash" in names

@pytest.mark.asyncio
async def test_plan_mode_denied_tool_returns_error():
    """在 plan 模式下，写入类工具需要审批（effect=ask）；当用户
    拒绝时，工具返回一个错误结果，而不会真正执行。"""
    from meharness.permissions import (
        DangerousCommandDetector,
        PathSandbox,
        PermissionChecker,
        PermissionMode,
        RuleEngine,
    )

    client = MockLLMClient([
        [
            TextDelta("Let me write..."),
            ToolCallComplete("t1", "WriteFile", {"file_path": "x.txt", "content": "hi"}),
            StreamEnd("end_turn", input_tokens=10, output_tokens=20),
        ],
        [
            TextDelta("OK, I can't write in plan mode."),
            StreamEnd("end_turn", input_tokens=30, output_tokens=15),
        ],
    ])
    registry = create_default_registry()
    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox("."),
        rule_engine=RuleEngine(),
        mode=PermissionMode.PLAN,
    )
    agent = Agent(client, registry, "anthropic", permission_checker=checker)
    agent.set_permission_mode(PermissionMode.PLAN)
    conv = ConversationManager()
    conv.add_user_message("Write a file")

    events = []
    async for e in agent.run(conv):
        events.append(e)
        # plan 模式在写入前会询问；这里模拟用户拒绝。
        if isinstance(e, PermissionRequest):
            e.future.set_result(PermissionResponse.DENY)

    c = _collect(events)
    assert len(c["tool_result"]) == 1
    assert c["tool_result"][0].is_error
    assert "denied" in c["tool_result"][0].output.lower() or "拒绝" in c["tool_result"][0].output
    assert len(c["error"]) == 0

def test_partition_tool_calls():
    """分批逻辑会把可并发执行的调用归到同一组。"""
    from meharness.tools.base import ToolCallComplete

    calls = [
        ToolCallComplete("1", "ReadFile", {}),
        ToolCallComplete("2", "ReadFile", {}),
        ToolCallComplete("3", "EditFile", {}),
        ToolCallComplete("4", "ReadFile", {}),
        ToolCallComplete("5", "ReadFile", {}),
    ]
    registry = create_default_registry()
    batches = partition_tool_calls(calls, registry)
    assert len(batches) == 3
    assert batches[0].concurrent and len(batches[0].calls) == 2
    assert not batches[1].concurrent and len(batches[1].calls) == 1
    assert batches[2].concurrent and len(batches[2].calls) == 2

def test_system_prompt_normal():
    sp = build_system_prompt()
    assert "Meharness" in sp
    assert "Plan mode" not in sp

def test_system_prompt_plan():
    reminder = build_plan_mode_reminder("/tmp/plan.md", False, 1)
    assert "Plan mode" in reminder
    assert "MUST NOT" in reminder

def test_plan_mode_sparse_reminder():
    reminder = build_plan_mode_reminder("/tmp/plan.md", True, 8)
    assert "Plan mode still active" in reminder

def test_environment_context():
    ctx = build_environment_context("/home/user/project")
    assert "/home/user/project" in ctx
    assert "Operating system" in ctx
    assert "Current time" in ctx

# ---------------------------------------------------------------------------
# run_to_completion 的 max_tokens 升级/恢复（无头/队友路径对齐交互式 run()）
# ---------------------------------------------------------------------------

class EscalatingMockClient(LLMClient):
    """首次响应撞 max_tokens；set_max_output_tokens 被调用后返回完整结果。"""

    def __init__(self) -> None:
        self.call_count = 0
        self.escalated = False

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.call_count += 1
        if self.call_count == 1:
            yield TextDelta("partial output before cap")
            yield StreamEnd("max_tokens", input_tokens=10, output_tokens=8192)
        else:
            yield TextDelta("full output after escalation")
            yield StreamEnd("end_turn", input_tokens=20, output_tokens=30)

    def set_max_output_tokens(self, tokens: int) -> None:
        self.escalated = True


@pytest.mark.asyncio
async def test_run_to_completion_escalates_max_tokens():
    """无头路径撞 max_tokens 时升级到 64k 同请求重试，不把截断当最终答案。"""
    client = EscalatingMockClient()
    agent = Agent(client, create_default_registry(), "anthropic", work_dir=".")
    result = await agent.run_to_completion("write a long response")
    assert client.escalated is True
    assert client.call_count == 2
    assert "full output after escalation" in result


class RecoveringMockClient(LLMClient):
    """连续撞顶两次（8k→64k 都撞）后恢复：验证部分输出被追加进对话再续写。"""

    def __init__(self) -> None:
        self.call_count = 0

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.call_count += 1
        if self.call_count == 1:
            yield TextDelta("first cut ")
            yield StreamEnd("max_tokens", input_tokens=10, output_tokens=8192)
        elif self.call_count == 2:
            yield TextDelta("second cut ")
            yield StreamEnd("max_tokens", input_tokens=10, output_tokens=64000)
        else:
            yield TextDelta("final full output")
            yield StreamEnd("end_turn", input_tokens=20, output_tokens=30)

    def set_max_output_tokens(self, tokens: int) -> None:
        pass


@pytest.mark.asyncio
async def test_run_to_completion_recovers_partial_output():
    """64k 仍撞顶时保留部分输出 + 续写消息，让模型接着写而不是从头。"""
    client = RecoveringMockClient()
    agent = Agent(client, create_default_registry(), "anthropic", work_dir=".")
    conv = ConversationManager()
    conv.add_user_message("long task")
    result = await agent.run_to_completion("", conv)
    assert client.call_count == 3
    assert "final full output" in result
    # 首次 8k 撞顶是同请求升级（不保留输出）；64k 再撞才把部分输出追加进对话续写
    assistant_texts = [m.content for m in conv.history if m.role == "assistant"]
    assert "second cut" in assistant_texts[0]
    assert "final full output" in assistant_texts[1]
