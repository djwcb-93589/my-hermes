"""同步和异步 AgentLoop 共用的观察事件安全摘要构造函数。"""

from __future__ import annotations


def build_post_llm_call_payload(
    *,
    finish_reason: str | None,
    has_text: bool,
    tool_call_count: int,
    token_usage: dict[str, int],
    duration_ms: int | float,
) -> dict[str, object]:
    """构造不包含模型响应正文的模型调用观察摘要。"""
    payload: dict[str, object] = {
        "finish_reason": (
            None if finish_reason is None else str(finish_reason)
        ),
        "has_text": bool(has_text),
        "tool_call_count": max(0, int(tool_call_count)),
        "duration_ms": max(0, int(duration_ms)),
    }
    if token_usage:
        payload["token_usage"] = dict(token_usage)
    return payload


def build_post_tool_call_payload(
    *,
    tool_name: str,
    tool_call_id: str,
    status: str,
    error_type: str | None,
    duration_ms: int | float,
) -> dict[str, object]:
    """构造不包含工具参数和输出的工具调用观察摘要。"""
    return {
        "tool_name": str(tool_name),
        "tool_call_id": str(tool_call_id),
        "status": str(status),
        "success": status == "succeeded",
        "error_type": error_type,
        "duration_ms": max(0, int(duration_ms)),
    }


def build_run_end_payload(
    *,
    status: str,
    stop_reason: str,
    iterations: int,
    tool_call_count: int,
    summary: str,
) -> dict[str, object]:
    """构造运行结束观察摘要，成功完成才允许标记最终回复。"""
    normalized_status = str(status)
    return {
        "status": normalized_status,
        "stop_reason": str(stop_reason),
        "iterations": max(0, int(iterations)),
        "tool_call_count": max(0, int(tool_call_count)),
        "has_final_reply": (
            normalized_status == "completed" and bool(summary)
        ),
    }
