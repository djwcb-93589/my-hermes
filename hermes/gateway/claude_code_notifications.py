"""Claude Code 通知合同到 Gateway system Outbox 的单向适配。"""

from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
import hashlib
import json

from hermes.claude_code.notification import (
    ClaudeCodeNotificationReceipt,
    ClaudeCodeNotificationTarget,
    ClaudeCodeTerminalNotification,
    render_claude_code_terminal_notification,
)
from hermes.claude_code.watch_registration import (
    ClaudeCodeWatchRegistrationResult,
)
from hermes.claude_code.watcher import (
    ClaudeCodeCompletionWatch,
    ClaudeCodeCompletionWatcherError,
    ClaudeCodeCompletionWatchState,
)
from hermes.gateway.system_notifications import (
    GatewaySystemNotificationPublisher,
)
from hermes.gateway.types import MessageEvent, SessionSource


class GatewayClaudeCodeNotificationPort:
    """把 Claude Code 终态通知入现有 Gateway Outbox，不直接调用平台 API。"""

    def __init__(self, publisher: GatewaySystemNotificationPublisher) -> None:
        if not isinstance(publisher, GatewaySystemNotificationPublisher):
            raise TypeError("publisher must be a GatewaySystemNotificationPublisher")
        self._publisher = publisher

    async def submit_terminal_notification(
        self,
        *,
        target: ClaudeCodeNotificationTarget,
        notification: ClaudeCodeTerminalNotification,
    ) -> ClaudeCodeNotificationReceipt:
        """渲染安全终态文本并以同一 notification_id 提交可靠投递。"""

        try:
            content = render_claude_code_terminal_notification(notification)
        except Exception:
            return ClaudeCodeNotificationReceipt(
                accepted=False,
                notification_id=notification.notification_id,
                retryable=False,
                error_type="terminal_notification_build_failed",
            )
        receipt = await self._publisher.publish(
            target=target,
            notification_id=notification.notification_id,
            content=content,
        )
        error_type = receipt.error_type
        if error_type == "notification_target_invalid":
            error_type = "watch_target_invalid"
        return ClaudeCodeNotificationReceipt(
            accepted=receipt.accepted,
            notification_id=receipt.notification_id,
            delivery_id=receipt.delivery_id,
            retryable=receipt.retryable,
            error_type=error_type,
        )


class GatewayClaudeCodeWatchRegistrationSink:
    """在 Tool 同步线程与当前 Gateway Watcher 之间建立受信边界。"""

    _REGISTRATION_TIMEOUT_SECONDS = 30.0
    _RECONCILIATION_TIMEOUT_SECONDS = 5.0

    def __init__(
        self,
        *,
        watcher,
        notification_target: ClaudeCodeNotificationTarget | None,
        loop: asyncio.AbstractEventLoop,
        initialization_error: str | None = None,
        readiness_provider=None,
    ) -> None:
        if not isinstance(loop, asyncio.AbstractEventLoop):
            raise TypeError("loop must be an event loop")
        self._watcher = watcher
        self._notification_target = notification_target
        self._loop = loop
        self._initialization_error = initialization_error
        if readiness_provider is not None and not callable(readiness_provider):
            raise TypeError("readiness_provider must be callable")
        self._readiness_provider = readiness_provider

    def register_start_result(
        self,
        *,
        result: object,
        session_owner: str,
    ) -> ClaudeCodeWatchRegistrationResult:
        """只接受成功 start 的公共结果，不保存 task 或 target 到 Tool。"""

        if self._initialization_error is not None:
            return ClaudeCodeWatchRegistrationResult(
                status="registration_failed",
                error_type=self._initialization_error,
                retryable=False,
            )
        if self._readiness_provider is not None:
            try:
                ready = bool(self._readiness_provider())
            except Exception:
                ready = False
            if not ready:
                return ClaudeCodeWatchRegistrationResult(
                    status="registration_failed",
                    error_type="watcher_unavailable",
                    retryable=True,
                )
        target = self._notification_target
        if target is None:
            return ClaudeCodeWatchRegistrationResult(
                status="registration_failed",
                error_type="watch_target_invalid",
                retryable=False,
            )
        if target.session_owner != session_owner:
            return ClaudeCodeWatchRegistrationResult(
                status="target_conflict",
                error_type="watch_owner_mismatch",
                retryable=False,
            )
        process_id = getattr(result, "process_id", None)
        round_id = getattr(result, "round_id", None)
        if (
            not isinstance(process_id, str)
            or not process_id.strip()
            or not isinstance(round_id, str)
            or not round_id.strip()
            or getattr(result, "initial_instruction_submitted", False)
            is not True
        ):
            return ClaudeCodeWatchRegistrationResult(
                status="not_registered_no_round",
                error_type="round_unavailable",
                retryable=False,
            )
        snapshot = getattr(result, "snapshot", None)
        session_ref = getattr(snapshot, "session_ref", None)
        if session_ref is None:
            return ClaudeCodeWatchRegistrationResult(
                status="not_registered_no_round",
                error_type="round_unavailable",
                retryable=False,
            )
        result_owner = getattr(session_ref, "session_owner", None)
        result_process_id = getattr(session_ref, "process_id", None)
        if (
            result_owner != session_owner
            or result_process_id != process_id
        ):
            return ClaudeCodeWatchRegistrationResult(
                status="target_conflict",
                error_type="watch_owner_mismatch",
                retryable=False,
            )
        watcher = self._watcher
        if watcher is None:
            return ClaudeCodeWatchRegistrationResult(
                status="registration_failed",
                error_type="watcher_unavailable",
                retryable=True,
            )
        if self._loop.is_closed():
            return ClaudeCodeWatchRegistrationResult(
                status="registration_failed",
                error_type="watcher_unavailable",
                retryable=True,
            )
        registration_coro = self._register_or_reuse(
            watcher=watcher,
            process_id=process_id,
            session_owner=session_owner,
            round_id=round_id,
            target=target,
        )
        try:
            future = asyncio.run_coroutine_threadsafe(
                registration_coro,
                self._loop,
            )
        except Exception:
            registration_coro.close()
            return ClaudeCodeWatchRegistrationResult(
                status="registration_failed",
                error_type="watcher_unavailable",
                retryable=True,
            )
        try:
            status, watch = future.result(
                timeout=self._REGISTRATION_TIMEOUT_SECONDS,
            )
        except FutureTimeoutError:
            # 取消只阻止尚未开始的协程，不能证明注册没有副作用；随后只读核对
            # 同一 owner/process/round/target，绝不自动再次 register。
            if not future.done():
                future.cancel()
            return self._reconcile_after_timeout(
                watcher=watcher,
                process_id=process_id,
                session_owner=session_owner,
                round_id=round_id,
                target=target,
            )
        except Exception as error:
            return self._watcher_error_result(error)
        if not isinstance(watch, ClaudeCodeCompletionWatch):
            return ClaudeCodeWatchRegistrationResult(
                status="registration_failed",
                error_type="watch_registration_failed",
                retryable=False,
            )
        return self._watch_registration_result(status, watch)

    async def _register_or_reuse(
        self,
        *,
        watcher,
        process_id: str,
        session_owner: str,
        round_id: str,
        target: ClaudeCodeNotificationTarget,
    ) -> tuple[str, ClaudeCodeCompletionWatch]:
        existing = await self._find_existing_watch(
            watcher=watcher,
            process_id=process_id,
            session_owner=session_owner,
            round_id=round_id,
            target=target,
        )
        if existing is not None:
            return "already_registered", existing
        try:
            watch = await watcher.register_watch(
                process_id=process_id,
                session_owner=session_owner,
                round_id=round_id,
                notification_target=target,
            )
        except ClaudeCodeCompletionWatcherError as error:
            if error.error_type != "watch_already_registered":
                raise
            existing = await self._find_existing_watch(
                watcher=watcher,
                process_id=process_id,
                session_owner=session_owner,
                round_id=round_id,
                target=target,
            )
            if existing is not None:
                return "already_registered", existing
            raise
        return "registered", watch

    def _reconcile_after_timeout(
        self,
        *,
        watcher,
        process_id: str,
        session_owner: str,
        round_id: str,
        target: ClaudeCodeNotificationTarget,
    ) -> ClaudeCodeWatchRegistrationResult:
        """超时后通过 Watcher 公共查询确认结果，不创建第二个 Watch。"""

        reconciliation_coro = self._find_existing_watch(
            watcher=watcher,
            process_id=process_id,
            session_owner=session_owner,
            round_id=round_id,
            target=target,
        )
        try:
            reconciliation = asyncio.run_coroutine_threadsafe(
                reconciliation_coro,
                self._loop,
            )
        except Exception:
            reconciliation_coro.close()
            return ClaudeCodeWatchRegistrationResult(
                status="registration_unknown",
                error_type="watch_registration_unconfirmed",
                retryable=True,
            )
        try:
            watch = reconciliation.result(
                timeout=self._RECONCILIATION_TIMEOUT_SECONDS,
            )
        except FutureTimeoutError:
            if not reconciliation.done():
                reconciliation.cancel()
            return ClaudeCodeWatchRegistrationResult(
                status="registration_unknown",
                error_type="watch_registration_unconfirmed",
                retryable=True,
            )
        except Exception as error:
            mapped = self._watcher_error_result(error)
            if mapped.status == "target_conflict":
                return mapped
            return ClaudeCodeWatchRegistrationResult(
                status="registration_unknown",
                error_type="watch_registration_unconfirmed",
                retryable=True,
            )
        if watch is None:
            return ClaudeCodeWatchRegistrationResult(
                status="registration_unknown",
                error_type="watch_registration_unconfirmed",
                retryable=True,
            )
        return self._watch_registration_result("already_registered", watch)

    @staticmethod
    def _watch_registration_result(
        status: str,
        watch: ClaudeCodeCompletionWatch,
    ) -> ClaudeCodeWatchRegistrationResult:
        """把 Watcher 的真实有限状态映射为安全注册结果。"""

        if not isinstance(watch, ClaudeCodeCompletionWatch):
            return ClaudeCodeWatchRegistrationResult(
                status="registration_failed",
                error_type="watch_registration_failed",
                retryable=False,
            )
        state = getattr(watch.state, "value", watch.state)
        confirmed_states = {
            ClaudeCodeCompletionWatchState.ACTIVE.value,
            ClaudeCodeCompletionWatchState.TERMINAL_DETECTED.value,
            ClaudeCodeCompletionWatchState.NOTIFICATION_PENDING.value,
            ClaudeCodeCompletionWatchState.NOTIFICATION_ACCEPTED.value,
        }
        if state in confirmed_states:
            return ClaudeCodeWatchRegistrationResult(
                status=status,
                registered=True,
                watch_id=watch.watch_id,
            )
        if state == ClaudeCodeCompletionWatchState.NOTIFICATION_FAILED.value:
            error_type = watch.last_error_type or "notification_failed"
            if not isinstance(error_type, str) or not error_type.strip():
                error_type = "notification_failed"
            return ClaudeCodeWatchRegistrationResult(
                status="registration_failed",
                error_type=error_type[:128],
                retryable=False,
            )
        if state == ClaudeCodeCompletionWatchState.CLOSED.value:
            error_type = watch.last_error_type
            if not isinstance(error_type, str) or not error_type.strip():
                return ClaudeCodeWatchRegistrationResult(
                    status="registration_unknown",
                    error_type="watch_closed_unconfirmed",
                    retryable=True,
                )
            return ClaudeCodeWatchRegistrationResult(
                status="registration_failed",
                error_type=error_type[:128],
                retryable=False,
            )
        return ClaudeCodeWatchRegistrationResult(
            status="registration_unknown",
            error_type="watch_state_unknown",
            retryable=True,
        )

    @staticmethod
    async def _find_existing_watch(
        *,
        watcher,
        process_id: str,
        session_owner: str,
        round_id: str,
        target: ClaudeCodeNotificationTarget,
    ) -> ClaudeCodeCompletionWatch | None:
        watches = await watcher.list_watches(include_closed=True)
        for watch in watches:
            if (
                watch.process_id != process_id
                or watch.session_owner != session_owner
                or watch.round_id != round_id
            ):
                continue
            if watch.target_id != target.target_id:
                raise ClaudeCodeCompletionWatcherError(
                    "watch_target_conflict",
                    "Claude Code completion watch is bound to another target",
                )
            return watch
        return None

    @staticmethod
    def _watcher_error_result(error: Exception) -> ClaudeCodeWatchRegistrationResult:
        if isinstance(error, ClaudeCodeCompletionWatcherError):
            error_type = error.error_type
            if error_type in {
                "watch_owner_mismatch",
                "watch_target_invalid",
                "watch_target_conflict",
            }:
                return ClaudeCodeWatchRegistrationResult(
                    status="target_conflict",
                    error_type=error_type,
                    retryable=False,
                )
            if error_type == "watch_already_registered":
                return ClaudeCodeWatchRegistrationResult(
                    status="registration_unknown",
                    error_type=error_type,
                    retryable=True,
                )
            if error_type in {
                "watch_round_unavailable",
                "watch_not_found",
            }:
                return ClaudeCodeWatchRegistrationResult(
                    status="not_registered_no_round",
                    error_type=error_type,
                    retryable=error.retryable,
                )
            if error_type in {
                "watcher_not_started",
                "watcher_shutting_down",
                "notification_port_unavailable",
            }:
                return ClaudeCodeWatchRegistrationResult(
                    status="registration_failed",
                    error_type="watcher_unavailable",
                    retryable=error.retryable,
                )
            return ClaudeCodeWatchRegistrationResult(
                status="registration_failed",
                error_type=error_type,
                retryable=error.retryable,
            )
        return ClaudeCodeWatchRegistrationResult(
            status="registration_failed",
            error_type="watch_registration_failed",
            retryable=False,
        )


def build_gateway_claude_code_notification_target(
    source: SessionSource,
    *,
    session_owner: str,
    reply_to_message_id: str | None = None,
) -> ClaudeCodeNotificationTarget:
    """从当前 Gateway 会话创建不含凭据的稳定跨层通知目标。"""

    if not isinstance(source, SessionSource):
        raise TypeError("source must be a SessionSource")
    platform = _require_target_text("platform", source.platform)
    chat_id = _require_target_text("chat_id", source.chat_id)
    session_owner = _require_target_text("session_owner", session_owner)
    account_id = _optional_target_text("account_id", source.account_id)
    thread_id = _optional_target_text("thread_id", source.thread_id)
    reply_to_message_id = _optional_target_text(
        "reply_to_message_id",
        reply_to_message_id,
    )
    metadata: dict[str, object] = {
        "platform": platform,
        "account_id": account_id,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "reply_to_message_id": reply_to_message_id,
    }
    canonical = json.dumps(
        metadata,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    target_id = "gateway-claude-code:" + hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()[:32]
    return ClaudeCodeNotificationTarget(
        target_id=target_id,
        metadata=metadata,
        session_owner=session_owner,
    )


def build_gateway_claude_code_notification_target_for_event(
    event: MessageEvent,
    *,
    session_owner: str,
) -> ClaudeCodeNotificationTarget:
    """保留当前入站消息的 reply 位置，但不复用其 Queue/Outbox 身份。"""

    if not isinstance(event, MessageEvent):
        raise TypeError("event must be a MessageEvent")
    return build_gateway_claude_code_notification_target(
        event.source,
        session_owner=session_owner,
        reply_to_message_id=event.message_id,
    )


def _require_target_text(field_name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1_024:
        raise ValueError(f"{field_name} must be a bounded non-empty string")
    return value


def _optional_target_text(field_name: str, value: object) -> str | None:
    if value is None or value == "":
        return None
    return _require_target_text(field_name, value)


__all__ = [
    "GatewayClaudeCodeNotificationPort",
    "GatewayClaudeCodeWatchRegistrationSink",
    "build_gateway_claude_code_notification_target",
    "build_gateway_claude_code_notification_target_for_event",
]
