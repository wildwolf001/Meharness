from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolUseBlock:
    tool_use_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False


@dataclass
class ThinkingBlock:
    thinking: str
    signature: str


@dataclass
class Message:
    role: str  # "user" | "assistant"
    content: str
    tool_uses: list[ToolUseBlock] = field(default_factory=list)
    tool_results: list[ToolResultBlock] = field(default_factory=list)
    thinking_blocks: list[ThinkingBlock] = field(default_factory=list)


# 本地 token 估算比率，参考 claude-code 的 roughTokenCountEstimation
# （services/tokenEstimation.ts）：普通文本按 UTF-8 字节数/4，JSON（工具参数）
# 按字节数/2。字节基准天然对 CJK 更准（中文每字 3 字节 → 0.75 token/字）。
_TEXT_BYTES_PER_TOKEN = 4.0
_JSON_BYTES_PER_TOKEN = 2.0


def _utf8_len(s: str) -> int:
    return len(s.encode("utf-8", errors="ignore"))


def _estimate_text_tokens(text: str) -> int:
    return int(_utf8_len(text) / _TEXT_BYTES_PER_TOKEN)


def _estimate_json_tokens(text: str) -> int:
    return int(_utf8_len(text) / _JSON_BYTES_PER_TOKEN)


def _message_tokens(m: Message) -> int:
    n = _estimate_text_tokens(m.content)
    for tb in m.thinking_blocks:
        n += _estimate_text_tokens(tb.thinking)
    for tu in m.tool_uses:
        n += _estimate_text_tokens(tu.tool_name) + _estimate_json_tokens(
            json.dumps(tu.arguments, ensure_ascii=False)
        )
    for tr in m.tool_results:
        n += _estimate_text_tokens(tr.content)
    return n


def estimate_tokens(messages: list[Message]) -> int:
    """对一组消息做粗略 token 估算（字节基准）。

    刻意做得粗略——它只覆盖那些尚未锚定到真实 API 用量数值的消息，这部分的
    精确度本就无关紧要。统计内容包括消息正文、thinking、工具调用参数以及
    工具结果内容。
    """
    return sum(_message_tokens(m) for m in messages)


@dataclass
class ConversationManager:
    history: list[Message] = field(default_factory=list)
    env_injected: bool = field(default=False, init=False)
    ltm_injected: bool = field(default=False, init=False)
    # API 报告的每轮真实 prompt 大小，保留用于向后兼容。
    # 现在与 baseline_tokens 一致（input + cache_read + cache_creation + output）。
    last_input_tokens: int = field(default=0, init=False)
    # 真实用量锚点。baseline_tokens 是上一轮 API 计费的完整 prompt+output 大小；
    # anchor_count 是记录该数值时的消息数量。两者配合让 current_tokens() 在
    # anchor_count 以内信任 API 数据，只对之后追加的消息做字符估算。
    # baseline_tokens == 0 表示"尚无锚点"（冷启动），此时退化为纯字符估算。
    baseline_tokens: int = field(default=0, init=False)
    anchor_count: int = field(default=0, init=False)

    def record_usage_anchor(
        self,
        input_tokens: int,
        output_tokens: int = 0,
        cache_read: int = 0,
        cache_creation: int = 0,
    ) -> None:
        """根据一次 API 响应钉下一个真实用量锚点。

        baseline = input + cache_read + cache_creation + output。各家服务商
        返回的 input_tokens 已经排除了命中缓存的 token，所以这三个 input 分量
        是相加关系，合起来才是真正的 prompt 大小；之所以再加上 output，是因为
        assistant 的回复此刻已成为历史的一部分。anchor_count 对齐到当前的消息
        数量，这样后续新追加的消息就成了唯一需要估算的部分。
        """
        self.baseline_tokens = (
            input_tokens + cache_read + cache_creation + output_tokens
        )
        self.anchor_count = len(self.history)
        # 保持旧字段同步，兼容仍在使用它的读取方。
        self.last_input_tokens = self.baseline_tokens

    def current_tokens(self) -> int:
        """对当前对话中的 token 数量做出最佳估算。

        有锚点时：baseline（真实用量）+ 仅对锚点之后追加的那些消息做字符估算。
        没有锚点时（冷启动，或刚经历一次压缩重置）：对整个历史做字符估算，
        这样在第一次 API 响应到来之前阈值检查依然能正常工作。
        """
        if self.baseline_tokens <= 0:
            return estimate_tokens(self.history)
        tail = self.history[self.anchor_count:]
        return self.baseline_tokens + estimate_tokens(tail)

    def add_user_message(self, content: str) -> None:
        self.history.append(Message(role="user", content=content))

    def add_assistant_message(
        self,
        content: str,
        tool_uses: list[ToolUseBlock] | None = None,
        thinking_blocks: list[ThinkingBlock] | None = None,
    ) -> None:
        self.history.append(
            Message(
                role="assistant",
                content=content,
                tool_uses=tool_uses or [],
                thinking_blocks=thinking_blocks or [],
            )
        )

    def add_system_reminder(self, content: str) -> None:
        self.history.append(
            Message(
                role="user",
                content=f"<system-reminder>\n{content}\n</system-reminder>",
            )
        )

    def add_tool_results_message(self, tool_results: list[ToolResultBlock]) -> None:
        self.history.append(
            Message(role="user", content="", tool_results=tool_results)
        )

    def synthesize_interrupted_tool_results(self) -> None:
        """中断时补齐 conversation 中悬空的 tool_use → tool_result 配对。

        参考 claude-code 的 yieldMissingToolResultBlocks / StreamingToolExecutor
        getRemainingResults：用户打断正在执行工具的回合时，已经 y 出的 assistant
        tool_use 必须有一条 user tool_result 承接，否则 resume 后 API 会收到
        "assistant 发了 tool_use 却没有 tool_result" 的非法序列，模型也看不到
        被中断的工具状态。

        从尾部找最近一条带 tool_uses 的 assistant 消息，为其中尚未被任何
        tool_results 消息承接的调用合成 is_error 结果。无悬空则不做任何事。
        """
        for i in range(len(self.history) - 1, -1, -1):
            msg = self.history[i]
            if msg.role != "assistant" or not msg.tool_uses:
                continue
            done_ids = {
                tr.tool_use_id
                for m in self.history[i + 1:]
                for tr in m.tool_results
            }
            pending = [
                tu.tool_use_id
                for tu in msg.tool_uses
                if tu.tool_use_id not in done_ids
            ]
            if pending:
                self.add_tool_results_message(
                    [
                        ToolResultBlock(
                            tool_use_id=tid,
                            content="Tool execution interrupted by user.",
                            is_error=True,
                        )
                        for tid in pending
                    ]
                )
            return


    def inject_environment(self, context: str) -> None:
        if not self.env_injected:
            self.history.insert(0, Message(role="user", content=context))
            self.env_injected = True

    def inject_long_term_memory(
        self, instructions: str, memories: str
    ) -> None:
        if self.ltm_injected:
            return
        sections: list[str] = []
        if instructions:
            sections.append(
                "# meharnessMd\n"
                "Codebase and user instructions are shown below. "
                "Be sure to adhere to these instructions. "
                "IMPORTANT: These instructions OVERRIDE any default behavior "
                "and you MUST follow them exactly as written.\n\n" + instructions
            )
        if memories:
            sections.append("# autoMemory\n" + memories)
        if not sections:
            return
        from datetime import date

        sections.append(f"# currentDate\nToday's date is {date.today().isoformat()}.")
        body = "\n\n".join(sections)
        wrapped = (
            "<system-reminder>\n"
            "As you answer the user's questions, you can use the following context:\n"
            + body
            + "\n\n      IMPORTANT: this context may or may not be relevant to your tasks."
            " You should not respond to this context unless it is highly relevant to your task.\n"
            "</system-reminder>"
        )
        pos = 1 if self.env_injected else 0
        self.history.insert(pos, Message(role="user", content=wrapped))
        self.ltm_injected = True

    def replace_history(self, new_messages: list[Message]) -> None:
        self.history = new_messages
        self.env_injected = False
        self.ltm_injected = False
        # 旧的用量锚点描述的是压缩前的对话记录，这里清除它，
        # 使 current_tokens() 退化为字符估算，直到下次 API 响应
        # 基于摘要后的历史重新建立锚点。
        self.baseline_tokens = 0
        self.anchor_count = 0
        self.last_input_tokens = 0


    def get_messages(self) -> list[Message]:
        return list(self.history)
