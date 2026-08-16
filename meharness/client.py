from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

# 流式超时（秒）：
# - 首字节（TTFB）：复杂任务上下文大，DeepSeek 可能数十秒才发首个 chunk，给足余量
# - 后续分片：已开始输出后 N 秒无字节才判挂起
_STREAM_TTFB_TIMEOUT = 300
_STREAM_IDLE_TIMEOUT = 120

from meharness.config import ProviderConfig
from meharness.conversation import ConversationManager
from meharness.serialization import (
    build_anthropic_messages,
    build_chat_completion_messages,
    build_openai_input,
)
from meharness.tools.base import (
    StreamEnd,
    StreamEvent,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    ToolCallComplete,
    ToolCallDelta,
    ToolCallStart,
)


# 限制自动拉取模型元数据的超时时间，防止慢响应或挂起的
# /v1/models 端点拖延启动。超时后降级为 None（即"未知"），
# 由下一层 context window 解析逻辑接管。
ANTHROPIC_MODEL_FETCH_TIMEOUT = 3.0


_EPHEMERAL = {"type": "ephemeral"}


def _mark_last_user_tail_for_cache(messages: list[dict[str, Any]]) -> None:
    """给最后一条 user 消息的最后一个 block 附加 cache_control。

    会原地修改 `messages`。Anthropic 会缓存到（且包含）这个 block 为止的前缀；
    后续请求只要前缀逐字节相同，缓存命中的 token 只需支付 10% 的费用。
    仅适用于 Anthropic 协议的消息。
    """
    if not messages:
        return
    # 从后往前找到最后一条 user 角色消息；assistant 尾部不能锚定 cache。
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            # 把字符串 content 升级为 block 形式，以便附加 cache_control。
            msg["content"] = [{
                "type": "text",
                "text": content,
                "cache_control": _EPHEMERAL,
            }]
        elif isinstance(content, list) and content:
            last = content[-1]
            if isinstance(last, dict):
                last["cache_control"] = _EPHEMERAL
        return


def _mark_last_tool_for_cache(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """返回一个浅拷贝的 tools 列表，并在最后一个 tool 上标记 cache_control。

    tool schema 在多轮对话之间是稳定的，因此标记列表尾部即可缓存整个 tool block。
    我们不直接修改调用方传入的列表，因为这些 tool schema 往往是注册表里的
    模块级单例。
    """
    if not tools:
        return tools
    marked = list(tools)
    last = dict(marked[-1])
    last["cache_control"] = _EPHEMERAL
    marked[-1] = last
    return marked


class LLMError(Exception):
    pass


def is_context_overflow_error(e: Exception) -> bool:
    """判断异常是否为"上下文超窗"（HTTP 413 或 message 提示 context/token 超长）。

    agent 循环用它触发响应式压缩并重试。
    """
    text = str(e).lower()
    if "413" in text or "context length" in text or "context_length" in text:
        return True
    if ("context" in text and any(w in text for w in ("exceed", "too large", "too long", "maximum", "limit"))):
        return True
    if ("token" in text and any(w in text for w in ("exceed", "too long", "maximum", "limit"))):
        return True
    return False


class AuthenticationError(LLMError):
    pass


class RateLimitError(LLMError):


    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class NetworkError(LLMError):
    pass


class LLMClient(ABC):
    @abstractmethod
    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield TextDelta("")

    def set_max_output_tokens(self, tokens: int) -> None:
        pass


def _supports_adaptive_thinking(model: str) -> bool:
    for family in ("claude-opus-4-", "claude-sonnet-4-"):
        if model.startswith(family):
            rest = model[len(family):]
            if rest and rest[0].isdigit() and int(rest[0]) >= 6:
                return True
    return False


class AnthropicClient(LLMClient):
    def __init__(self, config: ProviderConfig) -> None:
        self.model = config.model
        self.thinking = config.model_supports_thinking()
        self.thinking_budget = config.thinking_budget
        self.max_output_tokens = config.get_max_output_tokens()
        api_key = config.resolve_api_key()
        if not api_key:
            raise AuthenticationError(
                "Anthropic API key not found. "
                "Set it in .meharness/config.yaml or via ANTHROPIC_API_KEY env var."
            )
        self._client = AsyncAnthropic(api_key=api_key, base_url=config.base_url)

    def set_max_output_tokens(self, tokens: int) -> None:
        self.max_output_tokens = tokens

    async def fetch_model_context_window(self) -> int | None:
        """向 Anthropic 兼容的 /v1/models/{model} 端点查询模型的
        max_input_tokens（context window 解析的第 2 层）。

        采用尽力而为策略：遇到任何错误——非 anthropic 端点、网络故障、
        超时、字段缺失——都返回 ``None`` 而非抛出异常，以便调用方降级到
        下一层。它的阻塞时间不会超过 ANTHROPIC_MODEL_FETCH_TIMEOUT，也不会
        向外传播异常，因此在启动时调用是安全的。
        """
        try:
            info = await self._client.models.retrieve(
                self.model, timeout=ANTHROPIC_MODEL_FETCH_TIMEOUT
            )
            window = getattr(info, "max_input_tokens", None)
            if isinstance(window, int) and window > 0:
                return window
            return None
        except Exception:
            return None

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        import anthropic as _anthropic

        messages = build_anthropic_messages(conversation.get_messages())

        # 在最长稳定前缀上标记 prompt cache 断点：system、tools
        # 以及最后一条 user 消息的尾部。Anthropic 会缓存到每个断点，
        # 并在下次请求时按字节比对——context.manager 中的
        # ContentReplacementState 保证断点之后的 tool_result 内容保持稳定。
        _mark_last_user_tail_for_cache(messages)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }]
        if tools:
            kwargs["tools"] = _mark_last_tool_for_cache(tools)

        if self.thinking:
            if self.thinking_budget > 0:
                # 显式声明 budget：直接用它（参考 claude 能力声明 + budget 控制）
                kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": self.thinking_budget,
                }
            elif _supports_adaptive_thinking(self.model):
                kwargs["thinking"] = {"type": "enabled", "budget_tokens": 0}
            else:
                kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": max(self.max_output_tokens - 1, 1024),
                }

        current_tool_name = ""
        current_tool_id = ""
        json_accum = ""
        in_thinking = False
        thinking_accum = ""
        thinking_signature = ""

        try:
            async with self._client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    if event.type == "content_block_start":
                        block = event.content_block
                        if block.type == "thinking":
                            in_thinking = True
                            thinking_accum = ""
                            thinking_signature = ""
                        elif block.type == "tool_use":
                            current_tool_name = block.name
                            current_tool_id = block.id
                            json_accum = ""
                            yield ToolCallStart(
                                tool_name=current_tool_name,
                                tool_id=current_tool_id,
                            )
                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            yield TextDelta(text=delta.text)
                        elif delta.type == "thinking_delta":
                            thinking_accum += delta.thinking
                            yield ThinkingDelta(text=delta.thinking)
                        elif delta.type == "signature_delta":
                            thinking_signature = delta.signature
                        elif delta.type == "input_json_delta":
                            json_accum += delta.partial_json
                            yield ToolCallDelta(text=delta.partial_json)
                    elif event.type == "content_block_stop":
                        if in_thinking:
                            yield ThinkingComplete(
                                thinking=thinking_accum,
                                signature=thinking_signature,
                            )
                            in_thinking = False
                        if current_tool_name:
                            try:
                                args = json.loads(json_accum) if json_accum else {}
                            except json.JSONDecodeError:
                                args = {}
                            yield ToolCallComplete(
                                tool_id=current_tool_id,
                                tool_name=current_tool_name,
                                arguments=args,
                            )
                            current_tool_name = ""
                            current_tool_id = ""
                            json_accum = ""
                    elif event.type == "message_stop":
                        pass

                final = await stream.get_final_message()
                usage = final.usage
                yield StreamEnd(
                    stop_reason=final.stop_reason or "end_turn",
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read=getattr(usage, "cache_read_input_tokens", 0) or 0,
                    cache_creation=getattr(
                        usage, "cache_creation_input_tokens", 0
                    ) or 0,
                )

        except _anthropic.AuthenticationError as e:
            raise AuthenticationError(f"Invalid API key: {e}") from e
        except _anthropic.RateLimitError as e:
            retry = e.response.headers.get("retry-after") if e.response else None
            raise RateLimitError(
                f"Rate limited. {f'Retry after {retry}s.' if retry else 'Please wait.'}",
                retry_after=float(retry) if retry else None,
            ) from e
        except _anthropic.APIConnectionError as e:
            raise NetworkError(f"Network error: {e}") from e
        except _anthropic.APIStatusError as e:
            raise LLMError(f"API error ({e.status_code}): {e.message}") from e


class OpenAIClient(LLMClient):
    def __init__(self, config: ProviderConfig) -> None:
        self.model = config.model
        self.max_output_tokens = config.get_max_output_tokens()
        api_key = config.resolve_api_key()
        if not api_key:
            raise AuthenticationError(
                "OpenAI API key not found. "
                "Set it in .meharness/config.yaml or via OPENAI_API_KEY env var."
            )
        self._client = AsyncOpenAI(api_key=api_key, base_url=config.base_url)

    def set_max_output_tokens(self, tokens: int) -> None:
        self.max_output_tokens = tokens

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        import openai as _openai

        input_messages = build_openai_input(conversation.get_messages())

        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": input_messages,
            "stream": True,
        }
        if system:
            kwargs["instructions"] = system
        if tools:
            kwargs["tools"] = tools

        current_tool_name = ""
        current_call_id = ""
        json_accum = ""

        try:
            response_stream = await self._client.responses.create(**kwargs)
            async for event in response_stream:
                if event.type == "response.output_text.delta":
                    yield TextDelta(text=event.delta)
                elif event.type == "response.function_call_arguments.delta":
                    if not current_tool_name:
                        current_tool_name = getattr(event, "name", "") or ""
                        current_call_id = getattr(event, "call_id", "") or ""
                        if current_tool_name:
                            yield ToolCallStart(
                                tool_name=current_tool_name,
                                tool_id=current_call_id,
                            )
                    json_accum += event.delta
                    yield ToolCallDelta(text=event.delta)
                elif event.type == "response.function_call_arguments.done":
                    if not current_tool_name:
                        current_tool_name = getattr(event, "name", "") or ""
                        current_call_id = getattr(event, "call_id", "") or ""
                    try:
                        args = json.loads(json_accum) if json_accum else {}
                    except json.JSONDecodeError:
                        args = {}
                    yield ToolCallComplete(
                        tool_id=current_call_id,
                        tool_name=current_tool_name,
                        arguments=args,
                    )
                    current_tool_name = ""
                    current_call_id = ""
                    json_accum = ""
                elif event.type == "response.output_item.added":
                    item = getattr(event, "item", None)
                    if item and getattr(item, "type", "") == "function_call":
                        current_tool_name = getattr(item, "name", "")
                        current_call_id = getattr(item, "call_id", "")
                        json_accum = ""
                        yield ToolCallStart(
                            tool_name=current_tool_name,
                            tool_id=current_call_id,
                        )
                elif event.type == "response.completed":
                    resp = getattr(event, "response", None)
                    usage = getattr(resp, "usage", None) if resp else None
                    # Responses API 通过 input_tokens_details.cached_tokens
                    # 暴露 cache 命中数，没有 creation 计数。注意这里的
                    # input_tokens *包含*了缓存 token，所以需要减去它们，
                    # 保持 input + cache_read 可加性，与 Anthropic 对齐。
                    details = getattr(usage, "input_tokens_details", None)
                    cache_read = getattr(details, "cached_tokens", 0) or 0
                    input_tokens = getattr(usage, "input_tokens", 0) or 0
                    yield StreamEnd(
                        stop_reason="end_turn",
                        input_tokens=max(input_tokens - cache_read, 0),
                        output_tokens=getattr(usage, "output_tokens", 0) or 0,
                        cache_read=cache_read,
                        cache_creation=0,
                    )

        except _openai.AuthenticationError as e:
            raise AuthenticationError(f"Invalid API key: {e}") from e
        except _openai.RateLimitError as e:
            retry = None
            if hasattr(e, "response") and e.response is not None:
                retry = e.response.headers.get("retry-after")
            raise RateLimitError(
                f"Rate limited. {f'Retry after {retry}s.' if retry else 'Please wait.'}",
                retry_after=float(retry) if retry else None,
            ) from e
        except _openai.APIConnectionError as e:
            raise NetworkError(f"Network error: {e}") from e
        except _openai.APIStatusError as e:
            raise LLMError(f"API error ({e.status_code}): {e.message}") from e


class OpenAICompatClient(LLMClient):
    """面向 OpenAI 兼容 provider 的客户端，使用 Chat Completions API。

    与面向较新的 Responses API（``/responses``）的 ``OpenAIClient`` 不同，
    本客户端使用受广泛支持的 Chat Completions 端点（``/chat/completions``），
    因此能兼容任何暴露 OpenAI 兼容接口的 provider（例如 vLLM、Ollama、
    Together、Azure OpenAI 等）。
    """

    def __init__(self, config: ProviderConfig) -> None:
        self.model = config.model
        self.thinking = config.model_supports_thinking()
        self.thinking_budget = config.thinking_budget
        self.max_output_tokens = config.get_max_output_tokens()
        api_key = config.resolve_api_key()
        if not api_key:
            raise AuthenticationError(
                "OpenAI-compatible API key not found. "
                "Set it in .meharness/config.yaml or via OPENAI_API_KEY env var."
            )
        self._client = AsyncOpenAI(api_key=api_key, base_url=config.base_url)

    def set_max_output_tokens(self, tokens: int) -> None:
        self.max_output_tokens = tokens

    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把 tool schema 转换成 Chat Completions 格式。

        tool 注册表为 ``openai`` 系列输出的是 Responses API 风格的 dict::

            {"type": "function", "name": "...", "description": "...",
             "parameters": {...}}

        而 Chat Completions 要求把 name/description/parameters 嵌套在
        ``function`` 键下::

            {"type": "function", "function": {"name": "...",
             "description": "...", "parameters": {...}}}
        """
        converted: list[dict[str, Any]] = []
        for t in tools:
            converted.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", t.get("input_schema", {})),
                },
            })
        return converted

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        import openai as _openai

        messages = build_chat_completion_messages(conversation.get_messages())
        if system:
            messages = [{"role": "system", "content": system}] + messages
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_output_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = self._convert_tools(tools)

        try:
            # 流式优先；首字节超时/空响应（未产出任何内容）→ 非流式兜底
            # （参考 claude-code：流式失败回退一次性请求，规避兼容层流式怪癖）。
            yielded_any = False
            try:
                async for ev in self._stream_events(kwargs):
                    yielded_any = True
                    yield ev
            except NetworkError:
                if yielded_any:
                    raise  # 已产出部分内容，无法干净回退 → 交给上层重试
                async for ev in self._nonstream_events(kwargs):
                    yield ev
        except _openai.AuthenticationError as e:
            raise AuthenticationError(f"Invalid API key: {e}") from e
        except _openai.RateLimitError as e:
            retry = None
            if hasattr(e, "response") and e.response is not None:
                retry = e.response.headers.get("retry-after")
            raise RateLimitError(
                f"Rate limited. {f'Retry after {retry}s.' if retry else 'Please wait.'}",
                retry_after=float(retry) if retry else None,
            ) from e
        except _openai.APIConnectionError as e:
            raise NetworkError(f"Network error: {e}") from e
        except _openai.APIStatusError as e:
            raise LLMError(f"API error ({e.status_code}): {e.message}") from e

    async def _stream_events(
        self, kwargs: dict[str, Any]
    ) -> AsyncIterator[StreamEvent]:
        """流式请求：逐 chunk 解析成 StreamEvent。"""
        # 用于累积 streaming tool call 的状态。Chat Completions 流按
        # tool_calls 列表中的位置索引下发 delta，我们按索引跟踪每个进行中的调用。
        active_calls: dict[int, dict[str, str]] = {}
        thinking_accum = ""
        thinking_flushed = False
        last_usage = None
        saw_reasoning = False  # 收到过 reasoning_content（thinking=False 也计数）
        final_stop_reason = "end_turn"

        response = await self._client.chat.completions.create(**kwargs)
        # 空闲超时（自适应）：首字节给足 TTFB（复杂任务上下文大，DeepSeek 可能
        # 数十秒才发首个 chunk，120s 会误判挂起 → 触发重试叠加 → "卡住+开始 retry"）。
        response_iter = response.__aiter__()
        first_byte = True

        async def _next() -> Any:
            nonlocal first_byte
            timeout = _STREAM_TTFB_TIMEOUT if first_byte else _STREAM_IDLE_TIMEOUT
            first_byte = False
            try:
                return await asyncio.wait_for(
                    response_iter.__anext__(), timeout=timeout
                )
            except StopAsyncIteration:
                raise
            except asyncio.TimeoutError:
                raise NetworkError(
                    f"LLM stream idle timeout ({timeout}s no data)"
                ) from None

        while True:
            try:
                chunk = await _next()
            except StopAsyncIteration:
                break
            if chunk.usage:
                last_usage = chunk.usage
            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            delta = choice.delta

            # --- 思考内容（DeepSeek reasoning_content）---
            if delta and getattr(delta, "reasoning_content", None):
                saw_reasoning = True  # 即使 thinking=False 未显示也算"模型在推理"
                if self.thinking:
                    thinking_accum += delta.reasoning_content
                    yield ThinkingDelta(text=delta.reasoning_content)
                continue

            # --- 文本内容 ---
            if delta and delta.content:
                if self.thinking and thinking_accum and not thinking_flushed:
                    thinking_flushed = True
                    yield ThinkingComplete(thinking=thinking_accum, signature="")
                yield TextDelta(text=delta.content)

            # --- tool call 增量 ---
            if delta and delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in active_calls:
                        active_calls[idx] = {"id": "", "name": "", "args": ""}
                    call = active_calls[idx]
                    if tc.id:
                        call["id"] = tc.id
                    if tc.function and tc.function.name:
                        call["name"] = tc.function.name
                        yield ToolCallStart(tool_name=call["name"], tool_id=call["id"])
                    if tc.function and tc.function.arguments:
                        call["args"] += tc.function.arguments
                        yield ToolCallDelta(text=tc.function.arguments)

            # --- 结束原因 ---
            # 传播真实 stop_reason（openai-compat 的 finish_reason：stop/length/
            # tool_calls）——否则撞 max_tokens（length）被当成 end_turn，走不了
            # 64k 升级，模型部分输出被误判"空响应"重试。
            if choice.finish_reason:
                final_stop_reason = {
                    "stop": "end_turn",
                    "length": "max_tokens",
                    "tool_calls": "tool_use",
                }.get(choice.finish_reason, "end_turn")
            if choice.finish_reason in ("tool_calls", "stop"):
                if self.thinking and thinking_accum and not thinking_flushed:
                    thinking_flushed = True
                    yield ThinkingComplete(thinking=thinking_accum, signature="")
                if choice.finish_reason == "tool_calls":
                    for _idx, call in sorted(active_calls.items()):
                        try:
                            args = json.loads(call["args"]) if call["args"] else {}
                        except json.JSONDecodeError:
                            args = {}
                        yield ToolCallComplete(
                            tool_id=call["id"], tool_name=call["name"], arguments=args
                        )
                    active_calls.clear()

        # 兜底 StreamEnd：provider 若不送独立 usage-only chunk（DeepSeek 的
        # usage 常挂在最后 content chunk 上），循环结束后也必发。
        if last_usage is not None:
            details = getattr(last_usage, "prompt_tokens_details", None)
            cache_read = getattr(details, "cached_tokens", 0) or 0
            prompt_tokens = last_usage.prompt_tokens or 0
            yield StreamEnd(
                stop_reason=final_stop_reason,
                input_tokens=max(prompt_tokens - cache_read, 0),
                output_tokens=last_usage.completion_tokens or 0,
                cache_read=cache_read,
                cache_creation=0,
                saw_reasoning=saw_reasoning,
            )
        else:
            yield StreamEnd(
                stop_reason=final_stop_reason,
                input_tokens=0,
                output_tokens=0,
                cache_read=0,
                cache_creation=0,
                saw_reasoning=saw_reasoning,
            )

    async def _nonstream_events(
        self, kwargs: dict[str, Any]
    ) -> AsyncIterator[StreamEvent]:
        """非流式兜底：stream=False 一次性请求，产出与流式等价的 StreamEvent。"""
        nk = dict(kwargs)
        nk["stream"] = False
        nk.pop("stream_options", None)
        resp = await self._client.chat.completions.create(**nk)
        choice = resp.choices[0]
        msg = choice.message
        saw_reasoning = bool(getattr(msg, "reasoning_content", None))
        stop_reason = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
        }.get(choice.finish_reason, "end_turn")

        if getattr(msg, "reasoning_content", None) and self.thinking:
            yield ThinkingComplete(thinking=msg.reasoning_content, signature="")
        if msg.content:
            yield TextDelta(text=msg.content)
        for tc in (msg.tool_calls or []):
            try:
                args = (
                    json.loads(tc.function.arguments)
                    if tc.function and tc.function.arguments
                    else {}
                )
            except json.JSONDecodeError:
                args = {}
            yield ToolCallComplete(
                tool_id=tc.id or "",
                tool_name=tc.function.name if tc.function else "",
                arguments=args,
            )
        usage = resp.usage
        if usage is not None:
            details = getattr(usage, "prompt_tokens_details", None)
            cache_read = getattr(details, "cached_tokens", 0) or 0
            yield StreamEnd(
                stop_reason=stop_reason,
                input_tokens=max((usage.prompt_tokens or 0) - cache_read, 0),
                output_tokens=usage.completion_tokens or 0,
                cache_read=cache_read,
                cache_creation=0,
                saw_reasoning=saw_reasoning,
            )
        else:
            yield StreamEnd(
                stop_reason=stop_reason,
                input_tokens=0,
                output_tokens=0,
                cache_read=0,
                cache_creation=0,
                saw_reasoning=saw_reasoning,
            )


def create_client(config: ProviderConfig) -> LLMClient:
    if config.protocol == "anthropic":
        return AnthropicClient(config)
    elif config.protocol == "openai":
        return OpenAIClient(config)
    elif config.protocol == "openai-compat":
        return OpenAICompatClient(config)
    raise ValueError(f"Unknown protocol: {config.protocol}")


async def resolve_context_window(config: ProviderConfig) -> None:
    """context window 解析的第 2 层：对于 anthropic 协议的 provider，
    从 {base_url}/v1/models/{model} 自动拉取一次模型的 max_input_tokens，
    并通过 set_fetched_context_window 缓存到 ``config`` 上，这样后续
    config.get_context_window() 调用就能直接使用、无需再次访问网络。

    完全尽力而为，绝不抛出异常：非 anthropic provider、客户端构造失败
    （例如缺少 API key）、拉取失败或超时，都会让缓存保持不变，从而让
    get_context_window() 降级到内置映射表 / 默认值。在启动时调用是安全的——
    阻塞时间不会超过拉取自身的超时，也不会导致崩溃。
    """
    # 配置中显式指定的 window 在 get_context_window() 中优先级最高，
    # 上次调用已缓存的值也不需要重新拉取——直接跳过网络请求。
    if config.context_window > 0 or config._fetched_context_window > 0:
        return
    if config.protocol != "anthropic":
        return

    try:
        client = create_client(config)
    except Exception:
        return
    fetch = getattr(client, "fetch_model_context_window", None)
    if fetch is None:
        return

    try:
        window = await fetch()
    except Exception:
        window = None
    if window:
        config.set_fetched_context_window(window)
