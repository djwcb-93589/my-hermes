"""Claude Code Agent Tool 的可信上下文、owner 合同和 Controller 适配层。"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from hermes.claude_code.contracts import ClaudeCodeRuntimeError


CLAUDE_CODE_REQUIRED_TRUSTED_CONTEXT = "claude_code_invocation"
"""Tool schema 暴露前所需的现有 ToolPolicy trusted-context 标记。"""

CLAUDE_CODE_INVOCATION_PURPOSE = "managed_claude_code"
"""Grant 只能用于本受管 Claude Code Tool 边界。"""

_ALLOWED_ENVIRONMENTS = frozenset({"cli", "gateway"})
_MAX_OWNER_LENGTH = 1_024
_MAX_TURN_ID_LENGTH = 512
_MAX_PURPOSE_LENGTH = 128
_MAX_SOURCE_MESSAGE_LENGTH = 1_024
_MAX_GRANT_LIFETIME_SECONDS = 900.0


class ClaudeCodeAgentAdapterError(ClaudeCodeRuntimeError):
    """可信上下文或 Adapter 边界错误。"""


def _bounded_text(
    field_name: str,
    value: object,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise ValueError(f"{field_name} must be non-empty")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} exceeds the supported length")
    if any(ord(character) < 0x20 for character in normalized):
        raise ValueError(f"{field_name} contains control characters")
    return normalized


def _timestamp(field_name: str, value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{field_name} must be a non-negative timestamp")
    return float(value)


@dataclass(frozen=True, slots=True)
class ClaudeCodeOwner:
    """受信任的运行入口 owner，不接受 cwd、round 或 action 作为 owner。"""

    environment: str
    value: str

    def __post_init__(self) -> None:
        environment = _bounded_text(
            "environment",
            self.environment,
            maximum=32,
        ).lower()
        value = _bounded_text(
            "owner",
            self.value,
            maximum=_MAX_OWNER_LENGTH,
        )
        if environment not in _ALLOWED_ENVIRONMENTS:
            raise ValueError("environment is not supported for Claude Code")
        expected_prefix = f"{environment}:"
        if not value.startswith(expected_prefix) or len(value) <= len(
            expected_prefix
        ):
            raise ValueError("owner has an invalid environment prefix")
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "value", value)

    @classmethod
    def from_gateway_route_key(cls, route_key: str) -> "ClaudeCodeOwner":
        """为 Gateway 稳定 route 构造 owner；不使用可轮换 conversation_id。"""

        route = _bounded_text(
            "gateway_route_key",
            route_key,
            maximum=_MAX_OWNER_LENGTH - len("gateway:"),
        )
        return cls(environment="gateway", value=f"gateway:{route}")

    @classmethod
    def from_cli_session_key(cls, session_key: str) -> "ClaudeCodeOwner":
        """为 CLI 当前 session 构造 owner。"""

        session = _bounded_text(
            "cli_session_key",
            session_key,
            maximum=_MAX_OWNER_LENGTH - len("cli:"),
        )
        return cls(environment="cli", value=f"cli:{session}")

    @property
    def session_owner(self) -> str:
        """返回传给 Controller 的完整 owner 字符串。"""

        return self.value


@dataclass(frozen=True, slots=True)
class ClaudeCodeInvocationGrant:
    """不可由 Tool JSON 构造的短生命周期 Claude Code 调用授权。"""

    environment: str
    owner: ClaudeCodeOwner
    turn_id: str
    purpose: str
    created_at: float
    expires_at: float
    source_message_id: str | None = None
    _start_consumed: bool = field(
        default=False,
        init=False,
        repr=False,
        compare=False,
        hash=False,
    )
    _lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
        compare=False,
        hash=False,
    )

    def __post_init__(self) -> None:
        environment = _bounded_text(
            "environment",
            self.environment,
            maximum=32,
        ).lower()
        if environment not in _ALLOWED_ENVIRONMENTS:
            raise ValueError("environment is not supported for Claude Code")
        if not isinstance(self.owner, ClaudeCodeOwner):
            raise ValueError("owner must be a ClaudeCodeOwner")
        if self.owner.environment != environment:
            raise ValueError("grant environment does not match owner")
        turn_id = _bounded_text(
            "turn_id",
            self.turn_id,
            maximum=_MAX_TURN_ID_LENGTH,
        )
        purpose = _bounded_text(
            "purpose",
            self.purpose,
            maximum=_MAX_PURPOSE_LENGTH,
        )
        created_at = _timestamp("created_at", self.created_at)
        expires_at = _timestamp("expires_at", self.expires_at)
        if expires_at <= created_at:
            raise ValueError("expires_at must be later than created_at")
        if expires_at - created_at > _MAX_GRANT_LIFETIME_SECONDS:
            raise ValueError("grant lifetime exceeds the supported limit")
        source_message_id = self.source_message_id
        if source_message_id is not None:
            source_message_id = _bounded_text(
                "source_message_id",
                source_message_id,
                maximum=_MAX_SOURCE_MESSAGE_LENGTH,
            )
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "source_message_id", source_message_id)

    @property
    def start_consumed(self) -> bool:
        with self._lock:
            return self._start_consumed

    def validate(self, *, now: float | None = None) -> None:
        """验证 grant 的用途、owner、环境和时间范围。"""

        if self.environment not in _ALLOWED_ENVIRONMENTS:
            raise ClaudeCodeAgentAdapterError(
                "unsupported_environment",
                "Claude Code invocation environment is not supported",
            )
        if self.owner.environment != self.environment:
            raise ClaudeCodeAgentAdapterError(
                "owner_context_missing",
                "Claude Code invocation owner does not match its environment",
            )
        if self.purpose != CLAUDE_CODE_INVOCATION_PURPOSE:
            raise ClaudeCodeAgentAdapterError(
                "owner_context_missing",
                "Claude Code invocation grant purpose is invalid",
            )
        current = time.time() if now is None else _timestamp("now", now)
        if current < self.created_at or current >= self.expires_at:
            raise ClaudeCodeAgentAdapterError(
                "owner_context_missing",
                "Claude Code invocation grant is expired",
            )

    def consume_start(self, *, now: float | None = None) -> None:
        """原子消费一次 start 权限，拒绝同一 Grant 重复启动。"""

        self.validate(now=now)
        with self._lock:
            if self._start_consumed:
                raise ClaudeCodeAgentAdapterError(
                    "grant_reused",
                    "Claude Code start grant has already been consumed",
                )
            object.__setattr__(self, "_start_consumed", True)


class ClaudeCodeAgentAdapter:
    """只把受信 owner/grant 映射到 Controller 公共方法。"""

    def __init__(
        self,
        controller=None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        selected_controller = controller
        if selected_controller is None:
            # 延迟导入避免 claude_code 包初始化时形成循环依赖。
            from hermes.claude_code import get_claude_code_controller

            selected_controller = get_claude_code_controller()
        required_methods = (
            "start_task",
            "poll",
            "request_interrupt",
            "terminate",
        )
        if any(
            not callable(getattr(selected_controller, name, None))
            for name in required_methods
        ):
            raise TypeError(
                "controller must expose the Claude Code public workflow methods"
            )
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._controller = selected_controller
        self._clock = clock

    @property
    def controller(self):
        """供组合根做显式依赖注入检查；不暴露 Controller 私有状态。"""

        return self._controller

    def _validate_grant(self, grant: ClaudeCodeInvocationGrant) -> None:
        if not isinstance(grant, ClaudeCodeInvocationGrant):
            raise ClaudeCodeAgentAdapterError(
                "owner_context_missing",
                "Claude Code invocation grant is required",
            )
        grant.validate(now=self._clock())

    def start(
        self,
        *,
        grant: ClaudeCodeInvocationGrant,
        cwd: str,
        task: str,
        cancel_checker=None,
    ):
        self._validate_grant(grant)
        grant.consume_start(now=self._clock())
        return self._controller.start_task(
            user_requested=True,
            session_owner=grant.owner.session_owner,
            cwd=cwd,
            task=task,
            cancel_checker=cancel_checker,
        )

    def poll(
        self,
        *,
        grant: ClaudeCodeInvocationGrant,
        process_id: str,
        round_id: str | None = None,
        cancel_checker=None,
    ):
        self._validate_grant(grant)
        return self._controller.poll(
            session_owner=grant.owner.session_owner,
            process_id=process_id,
            round_id=round_id,
            cancel_checker=cancel_checker,
        )

    def request_interrupt(
        self,
        *,
        grant: ClaudeCodeInvocationGrant,
        process_id: str,
        round_id: str,
        cancel_checker=None,
    ):
        self._validate_grant(grant)
        return self._controller.request_interrupt(
            session_owner=grant.owner.session_owner,
            process_id=process_id,
            round_id=round_id,
            cancel_checker=cancel_checker,
        )

    def terminate(
        self,
        *,
        grant: ClaudeCodeInvocationGrant,
        process_id: str,
    ):
        self._validate_grant(grant)
        return self._controller.terminate(
            session_owner=grant.owner.session_owner,
            process_id=process_id,
        )


def create_gateway_claude_code_grant(
    *,
    route_key: str,
    turn_id: str,
    created_at: float,
    expires_at: float,
    source_message_id: str | None = None,
) -> ClaudeCodeInvocationGrant:
    """供未来 Gateway 受信入口显式签发 Grant；本阶段不自动调用。"""

    return ClaudeCodeInvocationGrant(
        environment="gateway",
        owner=ClaudeCodeOwner.from_gateway_route_key(route_key),
        turn_id=turn_id,
        purpose=CLAUDE_CODE_INVOCATION_PURPOSE,
        created_at=created_at,
        expires_at=expires_at,
        source_message_id=source_message_id,
    )


def create_cli_claude_code_grant(
    *,
    session_key: str,
    turn_id: str,
    created_at: float,
    expires_at: float,
    source_message_id: str | None = None,
) -> ClaudeCodeInvocationGrant:
    """供未来 CLI 受信入口显式签发 Grant；本阶段不自动调用。"""

    return ClaudeCodeInvocationGrant(
        environment="cli",
        owner=ClaudeCodeOwner.from_cli_session_key(session_key),
        turn_id=turn_id,
        purpose=CLAUDE_CODE_INVOCATION_PURPOSE,
        created_at=created_at,
        expires_at=expires_at,
        source_message_id=source_message_id,
    )


__all__ = [
    "CLAUDE_CODE_INVOCATION_PURPOSE",
    "CLAUDE_CODE_REQUIRED_TRUSTED_CONTEXT",
    "ClaudeCodeAgentAdapter",
    "ClaudeCodeAgentAdapterError",
    "ClaudeCodeInvocationGrant",
    "ClaudeCodeOwner",
    "create_cli_claude_code_grant",
    "create_gateway_claude_code_grant",
]
