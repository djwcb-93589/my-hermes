"""受管 Claude Code Tool：参数、Grant 和 Controller Adapter 的薄绑定。"""

from __future__ import annotations

import json
import threading

from hermes.claude_code.agent_adapter import (
    CLAUDE_CODE_GRANT_CONTEXT_KEY,
    ClaudeCodeAgentAdapter,
    ClaudeCodeAgentAdapterError,
    ClaudeCodeInvocationGrant,
)
from hermes.claude_code.contracts import ClaudeCodeRuntimeError
from hermes.tool_declarations.claude_code import TOOL_DECLARATIONS
from hermes.tools import register_declared_handlers


_ACTIONS = frozenset({
    "start",
    "poll",
    "request_interrupt",
    "terminate",
})
_ARGUMENTS_BY_ACTION = {
    "start": frozenset({"action", "cwd", "task"}),
    "poll": frozenset({"action", "process_id", "round_id"}),
    "request_interrupt": frozenset({
        "action",
        "process_id",
        "round_id",
    }),
    "terminate": frozenset({"action", "process_id"}),
}
_MAX_CWD_LENGTH = 4_096
_MAX_TASK_LENGTH = 65_535
_MAX_PROCESS_ID_LENGTH = 512
_MAX_ROUND_ID_LENGTH = 512

# 只暴露 Detector 已经生成的、对 Agent 有帮助且不会携带原生交互正文的元数据。
# 其余 metadata 仍属于 Controller 内部诊断信息，不进入 Tool 公共合同。
_SAFE_EVENT_METADATA_KEYS = frozenset(
    {
        "source",
        "ready_ui_only",
        "ui_non_activity",
        "input_echo_unconfirmed",
        "contains_input_echo",
        "context_complete",
        "event_text_truncated",
    }
)

_DEFAULT_ADAPTER_LOCK = threading.Lock()
_default_adapter: ClaudeCodeAgentAdapter | None = None

_ERROR_TYPE_ALIASES = {
    "controller_owner_mismatch": "owner_mismatch",
    "session_owner_mismatch": "owner_mismatch",
    "controller_task_not_found": "process_not_found",
    "session_not_found": "process_not_found",
    "controller_round_not_found": "round_mismatch",
    "cwd_required": "invalid_cwd",
}


class _ClaudeCodeToolArgumentError(ValueError):
    """Tool 参数错误，不触碰 Controller 生命周期。"""


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _empty_envelope(operation: str) -> dict:
    return {
        "ok": False,
        "operation": operation,
        "outcome": None,
        "state": None,
        "process_id": None,
        "cwd": None,
        "round_id": None,
        "initial_instruction_submitted": False,
        "process_active": False,
        "round_terminal": False,
        "raw_cursor": None,
        "events": [],
        "normalized_output": None,
        "observation_count": None,
        "consecutive_empty_reads": None,
        "output_used": None,
        "deadline_remaining": None,
        "action_required": None,
        "limits_hit": [],
        "error_type": None,
        "retryable": False,
        "delivery_unknown": False,
    }


def _error_envelope(
    operation: str,
    error_type: str,
    message: str,
    *,
    retryable: bool = False,
    delivery_unknown: bool = False,
) -> str:
    payload = _empty_envelope(operation)
    payload.update({
        "error_type": _ERROR_TYPE_ALIASES.get(error_type, error_type),
        "error": message,
        "retryable": bool(retryable),
        "delivery_unknown": bool(delivery_unknown),
    })
    return _json(payload)


def _normalize_controller_error_type(error_type: str) -> str:
    normalized = _ERROR_TYPE_ALIASES.get(error_type, error_type)
    if normalized.startswith("controller_"):
        return "controller_error"
    return normalized


def _safe_action(action) -> dict | None:
    if action is None:
        return None
    return {
        "action_id": action.action_id,
        "kind": action.kind.value,
        "summary": action.summary,
        "prompt_text": action.prompt_text,
        "options": list(action.options),
        "risk": action.risk,
        "cursor": action.cursor,
        "cursor_start": action.cursor_start,
        "cursor_end": action.cursor_end,
        "requires_user_input": True,
    }


def _safe_event_metadata(event) -> dict:
    """返回有界事件中明确允许公开的标量 metadata。"""

    metadata = getattr(event, "metadata", {})
    if not hasattr(metadata, "get"):
        return {}
    safe_metadata = {}
    for key in _SAFE_EVENT_METADATA_KEYS:
        value = metadata.get(key)
        if isinstance(value, (str, bool, int)):
            safe_metadata[key] = value
    return safe_metadata


def _safe_events(snapshot) -> list[dict]:
    """直接映射当前 Snapshot 事件，不在 Tool 层维护第二套增量状态。"""

    if snapshot is None:
        return []
    safe_events = []
    for event in snapshot.events:
        event_type = getattr(event.event_type, "value", event.event_type)
        if not isinstance(event_type, str):
            continue
        safe_events.append(
            {
                "type": event_type,
                "cursor_start": event.cursor_start,
                "cursor_end": event.cursor_end,
                "text": event.text,
                "metadata": _safe_event_metadata(event),
            }
        )
    return safe_events


def _result_envelope(operation: str, result) -> str:
    snapshot = result.snapshot
    session_ref = snapshot.session_ref if snapshot is not None else None
    # raw_cursor 是 Controller/Runtime 的绝对 cursor；Handler 不消费、不推进它。
    # events 仅来自这次 Controller observation 的公共 Snapshot，不重新读取日志或拼接历史。
    # normalized_output 是当前有界、脱敏的显示快照，不代表完整终端历史。
    # 后续调用方应以 Controller 返回的 process、round、cursor 和 action identity 为事实来源。
    payload = _empty_envelope(operation)
    payload.update({
        "ok": True,
        "outcome": result.outcome.value,
        "state": result.state.value,
        "process_id": session_ref.process_id if session_ref else None,
        "cwd": session_ref.cwd if session_ref else None,
        "round_id": result.round_id,
        "initial_instruction_submitted": result.initial_instruction_submitted,
        "process_active": result.process_active,
        "round_terminal": result.round_terminal,
        "raw_cursor": snapshot.raw_cursor if snapshot is not None else None,
        "events": _safe_events(snapshot),
        "normalized_output": (
            snapshot.normalized_output if snapshot is not None else None
        ),
        "observation_count": result.observation_count,
        "consecutive_empty_reads": result.consecutive_empty_reads,
        "output_used": result.output_used,
        "deadline_remaining": result.deadline_remaining,
        "action_required": _safe_action(result.action_required),
        "limits_hit": list(result.limits_hit),
    })
    return _json(payload)


def _default_claude_code_adapter() -> ClaudeCodeAgentAdapter:
    global _default_adapter
    with _DEFAULT_ADAPTER_LOCK:
        if _default_adapter is None:
            _default_adapter = ClaudeCodeAgentAdapter()
        return _default_adapter


def _bounded_argument(
    args: dict,
    name: str,
    *,
    maximum: int,
    required: bool,
) -> str | None:
    value = args.get(name)
    if value is None:
        if required:
            raise _ClaudeCodeToolArgumentError(f"{name} is required")
        return None
    if not isinstance(value, str):
        raise _ClaudeCodeToolArgumentError(f"{name} must be a string")
    normalized = value.strip()
    if required and not normalized:
        raise _ClaudeCodeToolArgumentError(f"{name} must be non-empty")
    if len(normalized) > maximum:
        raise _ClaudeCodeToolArgumentError(
            f"{name} exceeds the supported length"
        )
    return normalized


def _validate_args(args: object) -> tuple[str, dict]:
    if not isinstance(args, dict):
        raise _ClaudeCodeToolArgumentError("arguments must be an object")
    action = args.get("action")
    if not isinstance(action, str) or action not in _ACTIONS:
        raise _ClaudeCodeToolArgumentError("action is not supported")
    unknown = set(args).difference(_ARGUMENTS_BY_ACTION[action])
    if unknown:
        raise _ClaudeCodeToolArgumentError(
            "unsupported arguments for action: "
            + ", ".join(sorted(str(item) for item in unknown))
        )
    normalized = {"action": action}
    if action == "start":
        normalized["cwd"] = _bounded_argument(
            args,
            "cwd",
            maximum=_MAX_CWD_LENGTH,
            required=True,
        )
        normalized["task"] = _bounded_argument(
            args,
            "task",
            maximum=_MAX_TASK_LENGTH,
            required=True,
        )
    else:
        normalized["process_id"] = _bounded_argument(
            args,
            "process_id",
            maximum=_MAX_PROCESS_ID_LENGTH,
            required=True,
        )
        if action in {"poll", "request_interrupt"}:
            normalized["round_id"] = _bounded_argument(
                args,
                "round_id",
                maximum=_MAX_ROUND_ID_LENGTH,
                required=action == "request_interrupt",
            )
    return action, normalized


def _grant_from_context(kwargs: dict) -> ClaudeCodeInvocationGrant:
    if CLAUDE_CODE_GRANT_CONTEXT_KEY not in kwargs:
        raise ClaudeCodeAgentAdapterError(
            "claude_code_tool_disabled",
            "Claude Code Tool is disabled without a trusted invocation grant",
        )
    grant = kwargs.get(CLAUDE_CODE_GRANT_CONTEXT_KEY)
    if not isinstance(grant, ClaudeCodeInvocationGrant):
        raise ClaudeCodeAgentAdapterError(
            "owner_context_missing",
            "Claude Code Tool invocation grant has an invalid type",
        )
    return grant


def run_claude_code(args, *, adapter=None, **kwargs) -> str:
    """执行最小受管操作；所有生命周期判断交给 Controller。"""

    try:
        action, normalized = _validate_args(args)
    except _ClaudeCodeToolArgumentError as error:
        operation = (
            args.get("action", "unknown")
            if isinstance(args, dict)
            else "unknown"
        )
        return _error_envelope(
            "invalid" if operation not in _ACTIONS else operation,
            "invalid_args",
            str(error),
        )

    try:
        grant = _grant_from_context(kwargs)
        if not grant.allows_operation(action):
            return _error_envelope(
                action,
                "grant_operation_not_authorized",
                "Claude Code invocation grant does not allow this operation",
            )
        selected_adapter = adapter or _default_claude_code_adapter()
        cancel_checker = kwargs.get("cancel_checker")
        if cancel_checker is not None and not callable(cancel_checker):
            return _error_envelope(
                action,
                "invalid_args",
                "cancel_checker must be callable",
            )
        if action == "start":
            result = selected_adapter.start(
                grant=grant,
                cwd=normalized["cwd"],
                task=normalized["task"],
                cancel_checker=cancel_checker,
            )
        elif action == "poll":
            result = selected_adapter.poll(
                grant=grant,
                process_id=normalized["process_id"],
                round_id=normalized.get("round_id"),
                cancel_checker=cancel_checker,
            )
        elif action == "request_interrupt":
            result = selected_adapter.request_interrupt(
                grant=grant,
                process_id=normalized["process_id"],
                round_id=normalized["round_id"],
                cancel_checker=cancel_checker,
            )
        else:
            result = selected_adapter.terminate(
                grant=grant,
                process_id=normalized["process_id"],
            )
        return _result_envelope(action, result)
    except ClaudeCodeRuntimeError as error:
        return _error_envelope(
            action,
            _normalize_controller_error_type(error.error_type),
            error.safe_message,
            retryable=error.retryable,
            delivery_unknown=error.delivery_unknown,
        )


def register(registry, *, adapter=None) -> None:
    """注册默认关闭的 Claude Code declaration 和薄 handler。"""

    if adapter is not None and not isinstance(adapter, ClaudeCodeAgentAdapter):
        raise TypeError("adapter must be a ClaudeCodeAgentAdapter")

    def handler(args, **kwargs):
        return run_claude_code(args, adapter=adapter, **kwargs)

    register_declared_handlers(
        registry,
        TOOL_DECLARATIONS,
        {"claude_code": handler},
    )


__all__ = [
    "CLAUDE_CODE_GRANT_CONTEXT_KEY",
    "register",
    "run_claude_code",
]
