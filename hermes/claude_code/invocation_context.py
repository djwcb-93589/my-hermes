"""把显式请求转换为当前 Agent run 的最小 Claude Code 授权上下文。"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from hermes.claude_code.agent_adapter import (
    CLAUDE_CODE_GRANT_CONTEXT_KEY,
    CLAUDE_CODE_INTERACTION_SINK_CONTEXT_KEY,
    CLAUDE_CODE_REQUIRED_TRUSTED_CONTEXT,
    ClaudeCodeInvocationGrant,
    create_cli_claude_code_grant,
    create_gateway_claude_code_grant,
)
from hermes.claude_code.continuation import ClaudeCodeInteractionSink
from hermes.claude_code.request_detector import (
    ClaudeCodeExplicitRequest,
    ClaudeCodeRequestOperation,
    detect_claude_code_request,
)
from hermes.tools import ExecutionEnvironment, ToolPolicy


_GRANT_LIFETIME_SECONDS = 300.0
_CLAUDE_CODE_TOOLSET = "claude_code"
_ALLOWED_OPERATIONS_BY_REQUEST = {
    ClaudeCodeRequestOperation.START: frozenset({"start", "poll"}),
    ClaudeCodeRequestOperation.POLL: frozenset({"poll"}),
    ClaudeCodeRequestOperation.SEND_INSTRUCTION: frozenset(
        {"send_instruction", "poll"}
    ),
    ClaudeCodeRequestOperation.REQUEST_INTERRUPT: frozenset(
        {"request_interrupt", "poll"}
    ),
    ClaudeCodeRequestOperation.TERMINATE: frozenset({"terminate", "poll"}),
}


@dataclass(frozen=True, slots=True)
class ClaudeCodeInvocationContext:
    """当前单次 Agent run 的 Grant、动态 ToolPolicy 和私有上下文。"""

    request: ClaudeCodeExplicitRequest
    grant: ClaudeCodeInvocationGrant
    tool_policy: ToolPolicy
    tool_context: dict


def _expanded_tool_policy(
    base_policy: ToolPolicy,
    *,
    registry,
) -> ToolPolicy | None:
    """只在可信 CLI/Gateway 轮次中加入 claude_code toolset。"""

    if not isinstance(base_policy, ToolPolicy):
        return None
    if base_policy.environment not in {
        ExecutionEnvironment.CLI,
        ExecutionEnvironment.GATEWAY,
    }:
        return None
    if base_policy.unattended:
        return None
    try:
        if base_policy.enabled_toolsets is None:
            enabled_toolsets = set(
                registry.default_toolsets_for_policy(base_policy)
            )
        else:
            enabled_toolsets = set(base_policy.enabled_toolsets)
    except (TypeError, ValueError):
        return None
    enabled_toolsets.add(_CLAUDE_CODE_TOOLSET)
    trusted_context = set(base_policy.trusted_context)
    trusted_context.add(CLAUDE_CODE_REQUIRED_TRUSTED_CONTEXT)
    return ToolPolicy(
        environment=base_policy.environment,
        enabled_toolsets=frozenset(enabled_toolsets),
        unattended=False,
        trusted_context=frozenset(trusted_context),
        allowed_approval_modes=base_policy.allowed_approval_modes,
        max_risk_level=base_policy.max_risk_level,
    )


def _build_context(
    request: ClaudeCodeExplicitRequest,
    *,
    base_policy: ToolPolicy,
    registry,
    grant: ClaudeCodeInvocationGrant,
    originating_conversation_id: str | None = None,
    source_message_id: str | None = None,
) -> ClaudeCodeInvocationContext | None:
    dynamic_policy = _expanded_tool_policy(base_policy, registry=registry)
    if dynamic_policy is None:
        return None
    try:
        resolution = registry.resolve(dynamic_policy)
    except (AttributeError, TypeError, ValueError):
        return None
    if "claude_code" not in resolution.allowed_tool_names:
        return None
    sink = ClaudeCodeInteractionSink(
        environment=grant.environment,
        owner=grant.owner.session_owner,
        conversation_id=originating_conversation_id or grant.owner.session_owner,
        source_message_id=source_message_id,
    )
    return ClaudeCodeInvocationContext(
        request=request,
        grant=grant,
        tool_policy=dynamic_policy,
        tool_context={
            CLAUDE_CODE_GRANT_CONTEXT_KEY: grant,
            CLAUDE_CODE_INTERACTION_SINK_CONTEXT_KEY: sink,
        },
    )


def _request_grant_operations(
    request: ClaudeCodeExplicitRequest,
) -> frozenset[str]:
    return _ALLOWED_OPERATIONS_BY_REQUEST[request.operation]


def prepare_cli_claude_code_invocation(
    message: object,
    *,
    session_key: str,
    base_policy: ToolPolicy,
    registry,
    turn_id: str | None = None,
    now: float | None = None,
    originating_conversation_id: str | None = None,
    source_message_id: str | None = None,
) -> ClaudeCodeInvocationContext | None:
    """在 CLI 当前真实用户消息上原子准备一次性 Grant。"""

    request = detect_claude_code_request(message)
    if request is None or not isinstance(session_key, str) or not session_key.strip():
        return None
    created_at = time.time() if now is None else float(now)
    try:
        grant = create_cli_claude_code_grant(
            session_key=session_key,
            turn_id=turn_id or uuid.uuid4().hex,
            created_at=created_at,
            expires_at=created_at + _GRANT_LIFETIME_SECONDS,
            allowed_operations=_request_grant_operations(request),
        )
    except (TypeError, ValueError, OverflowError):
        return None
    return _build_context(
        request,
        base_policy=base_policy,
        registry=registry,
        grant=grant,
        originating_conversation_id=originating_conversation_id or session_key,
        source_message_id=source_message_id,
    )


def prepare_gateway_claude_code_invocation(
    message: object,
    *,
    route_key: str,
    source_message_id: str,
    base_policy: ToolPolicy,
    registry,
    turn_id: str | None = None,
    now: float | None = None,
    originating_conversation_id: str | None = None,
) -> ClaudeCodeInvocationContext | None:
    """在 Gateway 当前 MessageEvent 上原子准备一次性 Grant。"""

    request = detect_claude_code_request(message)
    if request is None:
        return None
    if (
        not isinstance(route_key, str)
        or not route_key.strip()
        or not isinstance(source_message_id, str)
        or not source_message_id.strip()
    ):
        return None
    created_at = time.time() if now is None else float(now)
    try:
        grant = create_gateway_claude_code_grant(
            route_key=route_key,
            turn_id=turn_id or uuid.uuid4().hex,
            created_at=created_at,
            expires_at=created_at + _GRANT_LIFETIME_SECONDS,
            source_message_id=source_message_id,
            allowed_operations=_request_grant_operations(request),
        )
    except (TypeError, ValueError, OverflowError):
        return None
    return _build_context(
        request,
        base_policy=base_policy,
        registry=registry,
        grant=grant,
        originating_conversation_id=originating_conversation_id or route_key,
        source_message_id=source_message_id,
    )


__all__ = [
    "ClaudeCodeInvocationContext",
    "prepare_cli_claude_code_invocation",
    "prepare_gateway_claude_code_invocation",
]
