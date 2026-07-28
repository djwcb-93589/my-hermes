"""受约束的控制型 Hook 结果及其合并规则。"""

from __future__ import annotations

from dataclasses import dataclass

from hermes.hooks.contracts import (
    HookEvent,
    HookInvocationResult,
)
from hermes.redaction import redact_explicit_secrets


_CONTROL_EVENT_NAMES = frozenset({"pre_llm_call", "pre_tool_call"})
_MAX_BLOCK_REASON_LENGTH = 300
_MAX_CONTEXT_LENGTH = 8_000
DEFAULT_MAX_ADD_CONTEXT_ITEMS = 8
DEFAULT_MAX_ADD_CONTEXT_CHARACTERS = 16_000
_CONTROL_FAILURE_REASON = "Hook control failed."


def _safe_text(value: object, *, max_length: int) -> str:
    """脱敏并限制控制文本，避免将回调细节扩散到核心流程。"""
    text = redact_explicit_secrets(value).strip()
    return text[:max_length].rstrip()


@dataclass(frozen=True, slots=True)
class Allow:
    """允许继续当前操作，不改变任何输入。"""


@dataclass(frozen=True, slots=True)
class Block:
    """阻止当前模型或工具调用的受限控制结果。"""

    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str):
            raise TypeError("Block reason must be a string")
        reason = _safe_text(self.reason, max_length=_MAX_BLOCK_REASON_LENGTH)
        if not reason:
            raise ValueError("Block reason must be a non-empty string")
        object.__setattr__(self, "reason", reason)


@dataclass(frozen=True, slots=True)
class AddContext:
    """向单次模型请求附加临时上下文的受限控制结果。"""

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("AddContext text must be a non-empty string")
        if len(self.text) > _MAX_CONTEXT_LENGTH:
            raise ValueError("AddContext text exceeds the maximum length")


ControlHookValue = Allow | Block | AddContext | None
"""控制型 Hook 唯一接受的回调返回值集合。"""


class HookControlError(ValueError):
    """控制型 Hook 返回值不符合当前事件契约时抛出。"""


@dataclass(frozen=True, slots=True)
class HookControlDispatchResult:
    """一次控制型分发的结构化、确定性汇总结果。"""

    event: HookEvent
    results: tuple[HookInvocationResult, ...]
    blocked: bool
    block_reason: str | None = None
    added_context: tuple[str, ...] = ()


def normalize_control_value(
    event: HookEvent,
    value: object,
) -> Allow | Block | AddContext:
    """校验回调返回值与当前控制事件是否兼容。"""
    if event.name not in _CONTROL_EVENT_NAMES:
        raise HookControlError("event is not a control hook event")
    if value is None:
        return Allow()
    if isinstance(value, Allow | Block):
        return value
    if isinstance(value, AddContext):
        if event.name == "pre_llm_call":
            return value
        raise HookControlError(
            "AddContext is only supported for pre_llm_call"
        )
    raise HookControlError(
        "hook control result must be Allow, Block, AddContext, or None"
    )


def control_failure_reason() -> str:
    """返回不暴露 Plugin 异常细节的统一控制失败原因。"""
    return _CONTROL_FAILURE_REASON


def add_context_within_budget(
    added_context: list[str],
    text: str,
    *,
    max_items: int,
    max_characters: int,
) -> bool:
    """检查单次 pre_llm_call 的临时上下文累计预算，不截断任何 Plugin 文本。"""
    return (
        len(added_context) < max_items
        and sum(len(item) for item in added_context) + len(text)
        <= max_characters
    )


def redact_added_context_results(
    results: list[HookInvocationResult],
) -> list[HookInvocationResult]:
    """在累计预算失败时移除已收集的 Plugin 文本，保留诊断结构。"""
    return [
        HookInvocationResult(
            hook_id=result.hook_id,
            success=result.success,
            value=None if isinstance(result.value, AddContext) else result.value,
            error_type=result.error_type,
            timed_out=result.timed_out,
            error_message=result.error_message,
        )
        for result in results
    ]


def control_error_message(exc: Exception) -> str:
    """为结构化执行结果提供脱敏且受限的错误信息。"""
    return (
        _safe_text(exc, max_length=_MAX_BLOCK_REASON_LENGTH)
        or _CONTROL_FAILURE_REASON
    )


def build_control_dispatch_result(
    event: HookEvent,
    results: list[HookInvocationResult],
    *,
    block_reason: str | None = None,
    added_context: list[str] | None = None,
) -> HookControlDispatchResult:
    """集中构造控制结果，避免同步和异步 Registry 行为漂移。"""
    return HookControlDispatchResult(
        event=event,
        results=tuple(results),
        blocked=block_reason is not None,
        block_reason=block_reason,
        added_context=tuple(added_context or ()),
    )
