"""Claude Code 通知合同到 Gateway system Outbox 的单向适配。"""

from __future__ import annotations

import hashlib
import json

from hermes.claude_code.notification import (
    ClaudeCodeNotificationReceipt,
    ClaudeCodeNotificationTarget,
    ClaudeCodeTerminalNotification,
    render_claude_code_terminal_notification,
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
    "build_gateway_claude_code_notification_target",
    "build_gateway_claude_code_notification_target_for_event",
]
