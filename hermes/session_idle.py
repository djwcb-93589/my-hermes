"""判断会话是否允许执行自动 idle 资源清理。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import math
import time
from typing import TYPE_CHECKING, Protocol, Sequence


if TYPE_CHECKING:
    from hermes.processes import ProcessCleanupReport


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


class SessionProcessLifecycle(SessionProcessView, Protocol):
    """自动 session 清理需要的最小 Process 生命周期接口。"""

    def cleanup_session(
        self,
        session_key: str,
    ) -> ProcessCleanupReport:
        """清理指定会话的 Process 资源并返回公共报告。"""


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


def _validate_session_key(session_key: object) -> str:
    """校验并返回可用于公开 Process 查询的 session key。"""

    if not isinstance(session_key, str) or not session_key.strip():
        raise ValueError("session_key must be a non-empty string")
    return session_key


def _validate_foreground_active(foreground_active: object) -> bool:
    """明确拒绝把其他真值对象当作前台状态。"""

    if not isinstance(foreground_active, bool):
        raise TypeError("foreground_active must be a boolean")
    return foreground_active


def _evaluate_session_release(
    session_key: str,
    *,
    foreground_active: bool,
    process_manager: SessionProcessView,
) -> SessionIdleDecision:
    """在参数已校验后判断前台与 Process 生命周期保护。"""

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

    session_key = _validate_session_key(session_key)
    last_activity = _finite_number(last_activity_at, "last_activity_at")
    idle_timeout = _finite_number(
        idle_timeout_seconds,
        "idle_timeout_seconds",
    )
    if idle_timeout < 0:
        raise ValueError("idle_timeout_seconds must be non-negative")
    foreground_active = _validate_foreground_active(foreground_active)
    evaluated_at = _finite_number(
        time.time() if now is None else now,
        "now",
    )

    if evaluated_at - last_activity < idle_timeout:
        return SessionIdleDecision(False, _NOT_EXPIRED)
    return _evaluate_session_release(
        session_key,
        foreground_active=foreground_active,
        process_manager=process_manager,
    )


def evaluate_session_release(
    session_key: str,
    *,
    foreground_active: bool,
    process_manager: SessionProcessView,
) -> SessionIdleDecision:
    """忽略 idle 时间，仅判断旧 session 运行资源能否安全释放。"""

    return _evaluate_session_release(
        _validate_session_key(session_key),
        foreground_active=_validate_foreground_active(foreground_active),
        process_manager=process_manager,
    )


__all__ = [
    "SessionIdleDecision",
    "SessionProcessLifecycle",
    "SessionProcessView",
    "evaluate_session_idle",
    "evaluate_session_release",
]
