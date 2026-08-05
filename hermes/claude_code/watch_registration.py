"""受管 Claude Code Completion Watch 注册的私有 run-local 合同。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


CLAUDE_CODE_WATCH_REGISTRATION_SINK_CONTEXT_KEY = (
    "_claude_code_watch_registration_sink"
)

_WATCH_REGISTRATION_STATUSES = frozenset(
    {
        "not_applicable",
        "not_registered_no_round",
        "registered",
        "already_registered",
        "registration_failed",
        "target_conflict",
    }
)
_MAX_WATCH_ID_LENGTH = 512
_MAX_ERROR_TYPE_LENGTH = 128


@dataclass(frozen=True, slots=True)
class ClaudeCodeWatchRegistrationResult:
    """Tool 可见的有限注册状态，不携带 NotificationTarget。"""

    status: str
    registered: bool = False
    watch_id: str | None = None
    error_type: str | None = None
    retryable: bool = False

    def __post_init__(self) -> None:
        if self.status not in _WATCH_REGISTRATION_STATUSES:
            raise ValueError("unsupported watch registration status")
        if not isinstance(self.registered, bool):
            raise ValueError("registered must be a boolean")
        if self.watch_id is not None and (
            not isinstance(self.watch_id, str)
            or not self.watch_id.strip()
            or len(self.watch_id) > _MAX_WATCH_ID_LENGTH
        ):
            raise ValueError("watch_id must be a bounded string or None")
        if self.registered and not self.watch_id:
            raise ValueError("registered watch requires watch_id")
        if self.error_type is not None and (
            not isinstance(self.error_type, str)
            or not self.error_type.strip()
            or len(self.error_type) > _MAX_ERROR_TYPE_LENGTH
        ):
            raise ValueError("error_type must be a bounded string or None")
        if not isinstance(self.retryable, bool):
            raise ValueError("retryable must be a boolean")

    def to_safe_dict(self) -> dict[str, object]:
        """返回可放入 Tool result 的安全投影。"""

        return {
            "status": self.status,
            "registered": self.registered,
            "watch_id": self.watch_id,
            "error_type": self.error_type,
            "retryable": self.retryable,
        }


@runtime_checkable
class ClaudeCodeWatchRegistrationSink(Protocol):
    """Gateway 组合层提供、Tool Handler 只依赖的最小注册边界。"""

    def register_start_result(
        self,
        *,
        result: object,
        session_owner: str,
    ) -> ClaudeCodeWatchRegistrationResult:
        """处理一次成功 start 结果并返回有限注册状态。"""


def not_applicable_watch_registration() -> ClaudeCodeWatchRegistrationResult:
    """CLI 或非 Gateway 环境的通知状态。"""

    return ClaudeCodeWatchRegistrationResult(status="not_applicable")


__all__ = [
    "CLAUDE_CODE_WATCH_REGISTRATION_SINK_CONTEXT_KEY",
    "ClaudeCodeWatchRegistrationResult",
    "ClaudeCodeWatchRegistrationSink",
    "not_applicable_watch_registration",
]
