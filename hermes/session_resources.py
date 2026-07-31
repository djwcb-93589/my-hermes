"""按可信会话标识统一释放运行期资源。"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, cast

from hermes.session_idle import (
    SessionIdleDecision,
    SessionProcessView,
    evaluate_session_idle,
)


if TYPE_CHECKING:
    from hermes.processes import ProcessCleanupReport, ProcessManager


logger = logging.getLogger(__name__)

_PROCESS_CLEANUP_ERROR = "process_cleanup_error"
_BROWSER_CLEANUP_ERROR = "browser_cleanup_error"
_BACKEND_CLEANUP_ERROR = "backend_cleanup_error"
_LIFECYCLE_BARRIER_ERROR = "lifecycle_barrier_incomplete"


@dataclass(frozen=True, slots=True)
class SessionResourceCleanupReport:
    """单个会话资源清理的不可变结果。"""

    process_cleanup: ProcessCleanupReport | None
    browser_cleanup_succeeded: bool
    backend_cleanup_attempted: bool
    backend_cleanup_succeeded: bool
    error_types: tuple[str, ...]

    @property
    def complete(self) -> bool:
        """仅在全部依赖步骤安全完成时返回 True。"""

        return (
            self.process_cleanup is not None
            and self.process_cleanup.complete
            and self.browser_cleanup_succeeded
            and self.backend_cleanup_attempted
            and self.backend_cleanup_succeeded
            and not self.error_types
        )

    @property
    def backend_cleanup_skipped(self) -> bool:
        """返回 Backend 是否因进程清理未完成而保留。"""

        return not self.backend_cleanup_attempted


@dataclass(frozen=True, slots=True)
class IdleSessionCleanupReport:
    """自动 idle 判断及其资源清理结果。"""

    attempted: bool
    decision: SessionIdleDecision
    resource_cleanup: SessionResourceCleanupReport | None

    @property
    def complete(self) -> bool:
        """未尝试时没有清理缺口；尝试后返回真实清理结果。"""

        if not self.attempted:
            return True
        return (
            self.resource_cleanup is not None
            and self.resource_cleanup.complete
        )


@dataclass(frozen=True, slots=True)
class GlobalResourceCleanupReport:
    """全部会话资源清理的不可变结果。"""

    process_cleanup: ProcessCleanupReport | None
    browser_cleanup_succeeded: bool
    backend_cleanup_attempted: bool
    backend_cleanup_succeeded: bool
    error_types: tuple[str, ...]

    @property
    def complete(self) -> bool:
        """仅在全部依赖步骤安全完成时返回 True。"""

        return (
            self.process_cleanup is not None
            and self.process_cleanup.complete
            and self.browser_cleanup_succeeded
            and self.backend_cleanup_attempted
            and self.backend_cleanup_succeeded
            and not self.error_types
        )

    @property
    def backend_cleanup_skipped(self) -> bool:
        """返回 Backend 是否因进程清理未完成而保留。"""

        return not self.backend_cleanup_attempted


def _log_cleanup_issue(
    scope: str,
    category: str,
    *,
    count: int,
    error: BaseException | None = None,
) -> None:
    """只记录稳定分类、数量和异常类型，避免泄漏底层消息。"""

    logger.warning(
        (
            "%s resource cleanup issue: "
            "category=%s count=%d exception_type=%s"
        ),
        scope,
        category,
        max(0, count),
        "none" if error is None else type(error).__name__,
    )


def _run_process_cleanup(
    process_manager: ProcessManager | None,
    *,
    session_key: str | None,
    scope: str,
) -> tuple[ProcessCleanupReport | None, tuple[str, ...]]:
    """延迟解析 ProcessManager，并校验它返回公共清理报告。"""

    try:
        from hermes.processes import (
            ProcessCleanupReport,
            process_manager as default_process_manager,
        )

        active_manager = (
            default_process_manager
            if process_manager is None
            else process_manager
        )
        if session_key is None:
            report = active_manager.cleanup_all()
        else:
            report = active_manager.cleanup_session(session_key)
        if not isinstance(report, ProcessCleanupReport):
            raise TypeError("Process cleanup returned an invalid report")
    except Exception as error:
        _log_cleanup_issue(
            scope,
            _PROCESS_CLEANUP_ERROR,
            count=1,
            error=error,
        )
        return None, (_PROCESS_CLEANUP_ERROR,)

    if not report.complete:
        _log_cleanup_issue(
            scope,
            "process_cleanup_incomplete",
            count=len(report.unresolved_process_ids),
        )
        return report, ("process_cleanup_incomplete",)
    return report, ()


def _cleanup_session_browser(session_key: str) -> tuple[bool, str | None]:
    """关闭单会话浏览器；异常只转换为稳定分类。"""

    try:
        from browser.runtime import default_browser_manager

        default_browser_manager.close_session(session_key)
    except Exception as error:
        _log_cleanup_issue(
            "Session",
            _BROWSER_CLEANUP_ERROR,
            count=1,
            error=error,
        )
        return False, _BROWSER_CLEANUP_ERROR
    return True, None


def _cleanup_all_browsers() -> tuple[bool, str | None]:
    """关闭全部浏览器；异常只转换为稳定分类。"""

    try:
        from browser.runtime import default_browser_manager

        default_browser_manager.close_all()
    except Exception as error:
        _log_cleanup_issue(
            "Global",
            _BROWSER_CLEANUP_ERROR,
            count=1,
            error=error,
        )
        return False, _BROWSER_CLEANUP_ERROR
    return True, None


def _cleanup_session_backend(session_key: str) -> tuple[bool, str | None]:
    """清理单会话 Backend；调用无异常即视为步骤完成。"""

    try:
        from hermes.backends import cleanup_backend

        cleanup_backend(session_key)
    except Exception as error:
        _log_cleanup_issue(
            "Session",
            _BACKEND_CLEANUP_ERROR,
            count=1,
            error=error,
        )
        return False, _BACKEND_CLEANUP_ERROR
    return True, None


def _cleanup_all_backends() -> tuple[bool, str | None]:
    """清理全部 Backend；调用无异常即视为步骤完成。"""

    try:
        from hermes.backends import cleanup_all_backends

        cleanup_all_backends()
    except Exception as error:
        _log_cleanup_issue(
            "Global",
            _BACKEND_CLEANUP_ERROR,
            count=1,
            error=error,
        )
        return False, _BACKEND_CLEANUP_ERROR
    return True, None


def cleanup_session_resources(
    session_key: str,
    *,
    process_manager: ProcessManager | None = None,
) -> SessionResourceCleanupReport:
    """按进程、浏览器、Backend 的依赖顺序清理一个会话。"""

    process_cleanup, process_error_types = _run_process_cleanup(
        process_manager,
        session_key=session_key,
        scope="Session",
    )
    browser_cleanup_succeeded, browser_error_type = (
        _cleanup_session_browser(session_key)
    )

    error_types = list(process_error_types)
    if browser_error_type is not None:
        error_types.append(browser_error_type)

    process_cleanup_complete = (
        process_cleanup is not None and process_cleanup.complete
    )
    backend_cleanup_attempted = process_cleanup_complete
    backend_cleanup_succeeded = False
    if process_cleanup_complete:
        backend_cleanup_succeeded, backend_error_type = (
            _cleanup_session_backend(session_key)
        )
        if backend_error_type is not None:
            error_types.append(backend_error_type)

    return SessionResourceCleanupReport(
        process_cleanup=process_cleanup,
        browser_cleanup_succeeded=browser_cleanup_succeeded,
        backend_cleanup_attempted=backend_cleanup_attempted,
        backend_cleanup_succeeded=backend_cleanup_succeeded,
        error_types=tuple(error_types),
    )


def cleanup_idle_session_resources(
    session_key: str,
    *,
    last_activity_at: float,
    idle_timeout_seconds: float,
    foreground_active: bool,
    process_manager: SessionProcessView,
) -> IdleSessionCleanupReport:
    """仅在共享 idle 策略允许时调用现有完整会话清理。"""

    decision = evaluate_session_idle(
        session_key,
        last_activity_at=last_activity_at,
        idle_timeout_seconds=idle_timeout_seconds,
        foreground_active=foreground_active,
        process_manager=process_manager,
    )
    if not decision.cleanup_allowed:
        return IdleSessionCleanupReport(
            attempted=False,
            decision=decision,
            resource_cleanup=None,
        )

    resource_cleanup = cleanup_session_resources(
        session_key,
        process_manager=cast("ProcessManager", process_manager),
    )
    return IdleSessionCleanupReport(
        attempted=True,
        decision=decision,
        resource_cleanup=resource_cleanup,
    )


def cleanup_all_session_resources(
    *,
    process_manager: ProcessManager | None = None,
    lifecycle_barrier_complete: bool = True,
) -> GlobalResourceCleanupReport:
    """按进程、浏览器、Backend 的依赖顺序清理全部会话。"""

    process_cleanup, process_error_types = _run_process_cleanup(
        process_manager,
        session_key=None,
        scope="Global",
    )
    browser_cleanup_succeeded, browser_error_type = _cleanup_all_browsers()

    error_types = list(process_error_types)
    if browser_error_type is not None:
        error_types.append(browser_error_type)
    if not lifecycle_barrier_complete:
        _log_cleanup_issue(
            "Global",
            _LIFECYCLE_BARRIER_ERROR,
            count=1,
        )
        error_types.append(_LIFECYCLE_BARRIER_ERROR)

    process_cleanup_complete = (
        process_cleanup is not None and process_cleanup.complete
    )
    backend_cleanup_attempted = (
        process_cleanup_complete and lifecycle_barrier_complete
    )
    backend_cleanup_succeeded = False
    if backend_cleanup_attempted:
        backend_cleanup_succeeded, backend_error_type = _cleanup_all_backends()
        if backend_error_type is not None:
            error_types.append(backend_error_type)

    return GlobalResourceCleanupReport(
        process_cleanup=process_cleanup,
        browser_cleanup_succeeded=browser_cleanup_succeeded,
        backend_cleanup_attempted=backend_cleanup_attempted,
        backend_cleanup_succeeded=backend_cleanup_succeeded,
        error_types=tuple(error_types),
    )


__all__ = [
    "GlobalResourceCleanupReport",
    "IdleSessionCleanupReport",
    "SessionResourceCleanupReport",
    "cleanup_all_session_resources",
    "cleanup_idle_session_resources",
    "cleanup_session_resources",
]
