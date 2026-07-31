"""判断会话是否允许执行自动 idle 资源清理。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import math
import time
from typing import Protocol, Sequence


logger = logging.getLogger(__name__)

_NOT_EXPIRED = "not_expired"
_FOREGROUND_ACTIVE = "foreground_active"
_ACTIVE_PROCESSES = "active_processes"
_PROCESS_STATE_UNKNOWN = "process_state_unknown"
_CLEANUP_ALLOWED = "cleanup_allowed"


class SessionProcessView(Protocol):
    """Session idle 策略需要的最小 Process 只读接口。"""

    def list(
        self,
        session_key: str,
        *,
        include_finished: bool = True,
    ) -> Sequence[object]:
        """返回当前会话的公开 Process 快照。"""


@dataclass(frozen=True, slots=True)
class SessionIdleDecision:
    """一次 Session idle 判断的稳定、不可变结果。"""

    cleanup_allowed: bool
    reason: str
    active_process_count: int = 0


def _finite_number(value: object, field_name: str) -> float:
    """把内部时间参数校验为有限数值，明确拒绝 bool。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{field_name} must be finite")
    return normalized


def _safe_session_digest(session_key: str) -> str:
    """返回不可逆的短会话标识，避免日志暴露原始 session key。"""

    return hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:12]


def evaluate_session_idle(
    session_key: str,
    *,
    last_activity_at: float,
    idle_timeout_seconds: float,
    foreground_active: bool,
    process_manager: SessionProcessView,
    now: float | None = None,
) -> SessionIdleDecision:
    """只通过公开 Process API 判断自动 idle cleanup 是否安全。"""

    if not isinstance(session_key, str) or not session_key.strip():
        raise ValueError("session_key must be a non-empty string")
    last_activity = _finite_number(last_activity_at, "last_activity_at")
    idle_timeout = _finite_number(
        idle_timeout_seconds,
        "idle_timeout_seconds",
    )
    if idle_timeout < 0:
        raise ValueError("idle_timeout_seconds must be non-negative")
    if not isinstance(foreground_active, bool):
        raise TypeError("foreground_active must be a boolean")
    evaluated_at = _finite_number(
        time.time() if now is None else now,
        "now",
    )

    if evaluated_at - last_activity < idle_timeout:
        return SessionIdleDecision(False, _NOT_EXPIRED)
    if foreground_active:
        return SessionIdleDecision(False, _FOREGROUND_ACTIVE)

    try:
        active_processes = process_manager.list(
            session_key,
            include_finished=False,
        )
        active_process_count = len(active_processes)
    except Exception as error:
        logger.warning(
            (
                "Session idle process state unavailable: "
                "session=%s exception_type=%s"
            ),
            _safe_session_digest(session_key),
            type(error).__name__,
        )
        return SessionIdleDecision(False, _PROCESS_STATE_UNKNOWN)

    if active_process_count:
        return SessionIdleDecision(
            False,
            _ACTIVE_PROCESSES,
            active_process_count=active_process_count,
        )
    return SessionIdleDecision(True, _CLEANUP_ALLOWED)


__all__ = [
    "SessionIdleDecision",
    "SessionProcessView",
    "evaluate_session_idle",
]
