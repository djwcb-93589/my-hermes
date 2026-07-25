"""同步模型流的 SDK 无关事件与完整回合累加器。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class StreamEvent:
    """向调用方报告一次模型回合的流式状态。"""

    event_type: str
    attempt_id: str
    delta: str | None = None


class AssistantMessageLike(Protocol):
    """完整模型消息进入 AgentLoop 时所需的最小只读属性。"""

    @property
    def content(self) -> str | None: ...

    @property
    def tool_calls(self) -> object: ...

    @property
    def reasoning_content(self) -> str | None: ...


@dataclass
class ModelFunctionCall:
    """完整工具调用中的函数信息。"""

    name: str
    arguments: str


@dataclass
class ModelToolCall:
    """完整工具调用，保持现有 AgentLoop 所需的属性访问方式。"""

    id: str
    function: ModelFunctionCall
    type: str = "function"


@dataclass
class ModelAssistantMessage:
    """完整 assistant 消息，不依赖供应商 SDK 的响应对象。"""

    content: str
    tool_calls: list[ModelToolCall] = field(default_factory=list)
    reasoning_content: str | None = None


@dataclass
class ModelTurnResult:
    """一次模型调用消费完成后的完整结果。"""

    assistant_message: AssistantMessageLike
    finish_reason: str | None
    usage: object | None = None


@dataclass
class _ToolCallParts:
    """按工具调用索引暂存尚未完成的字段碎片。"""

    id: str | None = None
    type: str | None = None
    name_parts: list[str] = field(default_factory=list)
    argument_parts: list[str] = field(default_factory=list)


def _value(obj, name: str, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class SynchronousStreamAccumulator:
    """把 OpenAI 兼容流的多个 chunk 重建为一条完整模型消息。"""

    def __init__(self, attempt_id: str | None = None) -> None:
        self._content_parts: list[str] = []
        self._reasoning_parts: list[str] = []
        self._tool_calls: dict[int, _ToolCallParts] = {}
        self._finish_reason: str | None = None
        self._usage = None
        self._attempt_id = attempt_id or uuid.uuid4().hex

    def add_chunk(self, chunk) -> tuple[str, str]:
        """累加一个 chunk，并返回其中新增的正文和推理文本。"""
        usage = _value(chunk, "usage")
        if usage is not None:
            self._usage = usage

        choices = _value(chunk, "choices") or []
        if not choices:
            return "", ""

        choice = choices[0]
        finish_reason = _value(choice, "finish_reason")
        if finish_reason is not None:
            self._finish_reason = str(finish_reason)

        delta = _value(choice, "delta")
        if delta is None:
            return "", ""

        content_delta = _value(delta, "content")
        reasoning_delta = _value(delta, "reasoning_content")
        content_text = "" if content_delta is None else str(content_delta)
        reasoning_text = "" if reasoning_delta is None else str(reasoning_delta)
        if content_text:
            self._content_parts.append(content_text)
        if reasoning_text:
            self._reasoning_parts.append(reasoning_text)

        tool_call_deltas = _value(delta, "tool_calls") or []
        for position, tool_call_delta in enumerate(tool_call_deltas):
            self._add_tool_call_delta(position, tool_call_delta)
        return content_text, reasoning_text

    def result(self) -> ModelTurnResult:
        """在所有 chunk 被消费后生成完整模型回合。"""
        tool_calls = []
        for index, parts in sorted(self._tool_calls.items()):
            tool_calls.append(
                ModelToolCall(
                    id=parts.id or self._fallback_tool_call_id(index),
                    type=parts.type or "function",
                    function=ModelFunctionCall(
                        name="".join(parts.name_parts),
                        arguments="".join(parts.argument_parts),
                    ),
                )
            )
        reasoning_content = "".join(self._reasoning_parts) or None
        return ModelTurnResult(
            assistant_message=ModelAssistantMessage(
                content="".join(self._content_parts),
                tool_calls=tool_calls,
                reasoning_content=reasoning_content,
            ),
            finish_reason=self._finish_reason or "stop",
            usage=self._usage,
        )

    def _add_tool_call_delta(self, position: int, tool_call_delta) -> None:
        raw_index = _value(tool_call_delta, "index", position)
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            index = position
        parts = self._tool_calls.setdefault(index, _ToolCallParts())

        tool_call_id = _value(tool_call_delta, "id")
        if tool_call_id and not parts.id:
            parts.id = str(tool_call_id)
        tool_call_type = _value(tool_call_delta, "type")
        if tool_call_type is not None:
            parts.type = str(tool_call_type)

        function = _value(tool_call_delta, "function")
        if function is None:
            return
        name = _value(function, "name")
        if name is not None:
            parts.name_parts.append(str(name))
        arguments = _value(function, "arguments")
        if arguments is not None:
            parts.argument_parts.append(str(arguments))

    def _fallback_tool_call_id(self, index: int) -> str:
        """为未提供 ID 的供应商生成本次尝试内稳定的工具调用 ID。"""
        return f"call_stream_{self._attempt_id}_{index}"
