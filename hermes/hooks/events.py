"""Hook 系统当前支持的固定观察事件。"""

from __future__ import annotations

from enum import Enum


class HookEventName(str, Enum):
    """P2 阶段允许注册和分发的观察型事件名称。"""

    POST_LLM_CALL = "post_llm_call"
    POST_TOOL_CALL = "post_tool_call"
    RUN_END = "run_end"


def normalize_observation_event_name(value: HookEventName | str) -> str:
    """校验 Plugin 使用的事件名称属于当前固定集合。"""
    try:
        return HookEventName(value).value
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unsupported hook event name: {value!r}") from exc
