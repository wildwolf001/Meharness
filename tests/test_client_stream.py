# OpenAICompatClient 流式：usage 兜底 StreamEnd 测试。
# 回归：DeepSeek 等 provider 可能把 usage 挂在最后一个 content chunk（choices
# 非空）而不是独立 usage-only chunk，旧代码因此永远不发 StreamEnd，导致
# "✻ Done (in 0 · out 0)"（usage 缺失）——流结束后必须兜底 yield StreamEnd。

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from meharness.client import OpenAICompatClient
from meharness.config import ProviderConfig
from meharness.tools.base import StreamEnd, TextDelta


def _cfg() -> ProviderConfig:
    return ProviderConfig(
        name="test",
        protocol="openai-compat",
        base_url="https://example.com/v1",
        model="test-model",
        api_key="sk-test",
    )


class _AsyncSeq:
    def __init__(self, items):
        self._it = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _FakeResponse:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return _AsyncSeq(self._chunks)


async def _collect(client, chunks):
    """用 fake chunk 列表跑 client.stream，返回 (文本, StreamEnd)。"""
    fake_resp = _FakeResponse(chunks)
    client._client.chat.completions.create = AsyncMock(return_value=fake_resp)
    from meharness.conversation import ConversationManager

    conv = ConversationManager()
    text = ""
    end: StreamEnd | None = None
    async for ev in client.stream(conv, system=""):
        if isinstance(ev, TextDelta):
            text += ev.text
        elif isinstance(ev, StreamEnd):
            end = ev
    return text, end


def _chunk(content=None, usage=None, finish_reason=None, empty_choices=False):
    if empty_choices:
        return SimpleNamespace(choices=[], usage=usage)
    delta = SimpleNamespace(content=content, tool_calls=None, reasoning_content=None)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage)


@pytest.mark.asyncio
async def test_usage_on_last_content_chunk_still_yields_stream_end() -> None:
    """usage 挂在最后一个 content chunk（choices 非空）时，仍必须发 StreamEnd。"""
    client = OpenAICompatClient(_cfg())
    usage = SimpleNamespace(
        prompt_tokens=30,
        completion_tokens=15,
        prompt_tokens_details=SimpleNamespace(cached_tokens=5),
    )
    chunks = [
        _chunk(content="hel", usage=None),
        _chunk(content="lo", usage=usage, finish_reason="stop"),
    ]
    text, end = await _collect(client, chunks)
    assert text == "hello"
    assert end is not None
    assert end.input_tokens == 25  # 30 - 5 cache
    assert end.output_tokens == 15
    assert end.cache_read == 5


@pytest.mark.asyncio
async def test_no_usage_anywhere_still_yields_stream_end() -> None:
    """provider 完全不送 usage 时也要兜底发 StreamEnd（tokens 为 0 而非缺失）。"""
    client = OpenAICompatClient(_cfg())
    chunks = [_chunk(content="ok", usage=None, finish_reason="stop")]
    text, end = await _collect(client, chunks)
    assert text == "ok"
    assert end is not None
    assert end.input_tokens == 0
    assert end.output_tokens == 0


@pytest.mark.asyncio
async def test_stream_idle_timeout_raises(monkeypatch) -> None:
    """流式 120s（测试里调小）无任何字节 → 抛 NetworkError 而不是挂死。"""
    import asyncio

    import meharness.client as C

    monkeypatch.setattr(C, "_STREAM_IDLE_TIMEOUT", 0.05)
    client = OpenAICompatClient(_cfg())

    class _Never:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(10)  # 永不返回

    client._client.chat.completions.create = AsyncMock(return_value=_Never())
    from meharness.conversation import ConversationManager

    with pytest.raises(C.NetworkError):
        async for _ in client.stream(ConversationManager(), system=""):
            pass


@pytest.mark.asyncio
async def test_usage_only_chunk_still_yields_stream_end() -> None:
    """传统独立 usage-only chunk（choices 空）路径保持可用。"""
    client = OpenAICompatClient(_cfg())
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=4,
        prompt_tokens_details=None,
    )
    chunks = [
        _chunk(content="hi", usage=None),
        _chunk(content=None, usage=usage, empty_choices=True),  # 独立 usage-only chunk
    ]
    text, end = await _collect(client, chunks)
    assert text == "hi"
    assert end is not None
    assert end.input_tokens == 10
    assert end.output_tokens == 4
