"""Hook 系统当前支持的固定控制型和观察型事件。"""

from __future__ import annotations

from enum import Enum


class HookEventName(str, Enum):
    """P3 阶段允许注册和分发的固定事件名称。"""

    PRE_LLM_CALL = "pre_llm_call"
    PRE_TOOL_CALL = "pre_tool_call"
    POST_LLM_CALL = "post_llm_call"
    POST_TOOL_CALL = "post_tool_call"
    RUN_END = "run_end"


def normalize_hook_event_name(value: HookEventName | str) -> str:
    """校验 Plugin 使用的事件名称属于当前固定集合。"""
    try:
        return HookEventName(value).value
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unsupported hook event name: {value!r}") from exc


def normalize_observation_event_name(value: HookEventName | str) -> str:
    """兼容旧名称；P3 起固定事件集合同时包含控制型事件。"""
    return normalize_hook_event_name(value)
