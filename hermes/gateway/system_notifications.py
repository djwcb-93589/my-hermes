"""复用 Gateway Outbox 的平台无关系统通知发布器。"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from hermes.db import enqueue_gateway_outbox, get_gateway_outbox
from hermes.gateway.adapters import BasePlatformAdapter
from hermes.gateway.persistence import GatewayPersistence


SYSTEM_NOTIFICATION_DELIVERY_KIND = "system_notification"
_MAX_TARGET_ID_CHARS = 512
_MAX_NOTIFICATION_ID_CHARS = 512
_MAX_CONTENT_CHARS = 32_768


@runtime_checkable
class GatewaySystemNotificationTarget(Protocol):
    """系统通知只依赖不透明目标身份和 Gateway 自己定义的元数据。"""

    target_id: str
    metadata: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class GatewaySystemNotificationReceipt:
    """入 Outbox 的结果；accepted 不表示平台网络已经送达。"""

    accepted: bool
    notification_id: str
    delivery_id: str | None = None
    retryable: bool = False
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class _GatewayNotificationDestination:
    platform: str
    chat_id: str
    thread_id: str | None
    reply_to_message_id: str | None


class GatewaySystemNotificationPublisher:
    """把系统通知可靠入 Gateway Outbox，不执行平台网络发送。"""

    def __init__(
        self,
        *,
        persistence: GatewayPersistence,
        adapter_provider: Callable[[str], BasePlatformAdapter | None],
        runtime_fence_provider: Callable[[], dict[str, object] | None],
        runtime_lease_valid: Callable[[], bool],
        gateway_running: Callable[[], bool],
        outbox_launcher: Callable[[str, str], Awaitable[None]],
    ) -> None:
        if not isinstance(persistence, GatewayPersistence):
            raise TypeError("persistence must be a GatewayPersistence")
        for name, value in (
            ("adapter_provider", adapter_provider),
            ("runtime_fence_provider", runtime_fence_provider),
            ("runtime_lease_valid", runtime_lease_valid),
            ("gateway_running", gateway_running),
            ("outbox_launcher", outbox_launcher),
        ):
            if not callable(value):
                raise TypeError(f"{name} must be callable")
        self._persistence = persistence
        self._adapter_provider = adapter_provider
        self._runtime_fence_provider = runtime_fence_provider
        self._runtime_lease_valid = runtime_lease_valid
        self._gateway_running = gateway_running
        self._outbox_launcher = outbox_launcher

    async def publish(
        self,
        *,
        target: GatewaySystemNotificationTarget,
        notification_id: str,
        content: str,
    ) -> GatewaySystemNotificationReceipt:
        """准备 payload、持久化同一稳定通知，并唤醒既有 Outbox worker。"""

        try:
            destination = self._destination(target)
            notification_id = self._require_text(
                "notification_id",
                notification_id,
                _MAX_NOTIFICATION_ID_CHARS,
            )
            content = self._require_text("content", content, _MAX_CONTENT_CHARS)
        except ValueError:
            return GatewaySystemNotificationReceipt(
                accepted=False,
                notification_id=(
                    notification_id
                    if isinstance(notification_id, str) and notification_id
                    else "invalid-notification-id"
                ),
                retryable=False,
                error_type="notification_target_invalid",
            )
        if not self._gateway_can_accept():
            return GatewaySystemNotificationReceipt(
                accepted=False,
                notification_id=notification_id,
                retryable=True,
                error_type="notification_port_unavailable",
            )

        adapter = self._adapter_provider(destination.platform)
        if adapter is None:
            return GatewaySystemNotificationReceipt(
                accepted=False,
                notification_id=notification_id,
                retryable=True,
                error_type="notification_port_unavailable",
            )
        delivery_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"hermes:system-notification:{notification_id}",
            )
        )
        try:
            payloads = adapter.prepare_outbound(content, delivery_id=delivery_id)
        except Exception:
            return GatewaySystemNotificationReceipt(
                accepted=False,
                notification_id=notification_id,
                retryable=False,
                error_type="notification_enqueue_failed",
            )
        if not isinstance(payloads, list) or not payloads:
            return GatewaySystemNotificationReceipt(
                accepted=False,
                notification_id=notification_id,
                retryable=False,
                error_type="notification_enqueue_failed",
            )

        source_identity = f"system-notification:{notification_id}"
        # route_key 同样锚定 notification identity，防止同一通知被改投到新 target。
        route_key = f"system-notification:{notification_id}"
        outbox = {
            "id": delivery_id,
            "route_key": route_key,
            # 独立系统 identity 不会触碰触发 CC 的入站 Queue 状态。
            "source_message_id": source_identity,
            "queue_message_id": source_identity,
            "event_json": json.dumps(
                {
                    "origin_kind": "system_notification",
                    "notification_id": notification_id,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "platform": destination.platform,
            "chat_id": destination.chat_id,
            "reply_to_message_id": destination.reply_to_message_id,
            "thread_id": destination.thread_id,
            "delivery_kind": SYSTEM_NOTIFICATION_DELIVERY_KIND,
            "payloads": payloads,
        }
        fence = self._active_fence()
        if fence is None:
            return GatewaySystemNotificationReceipt(
                accepted=False,
                notification_id=notification_id,
                retryable=True,
                error_type="notification_port_unavailable",
            )
        try:
            actual_delivery_id = await self._persistence.call(
                enqueue_gateway_outbox,
                outbox,
                **fence,
            )
            persisted = await self._persistence.call(
                get_gateway_outbox,
                actual_delivery_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return GatewaySystemNotificationReceipt(
                accepted=False,
                notification_id=notification_id,
                retryable=True,
                error_type="notification_enqueue_failed",
            )
        if not self._matches_persisted_outbox(persisted, outbox):
            return GatewaySystemNotificationReceipt(
                accepted=False,
                notification_id=notification_id,
                retryable=False,
                error_type="notification_identity_mismatch",
            )
        try:
            await self._outbox_launcher(actual_delivery_id, route_key)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Outbox 已经可靠入库；下一轮恢复仍会使用现有 worker 投递。
            pass
        return GatewaySystemNotificationReceipt(
            accepted=True,
            notification_id=notification_id,
            delivery_id=actual_delivery_id,
        )

    def _gateway_can_accept(self) -> bool:
        try:
            return bool(self._gateway_running() and self._runtime_lease_valid())
        except Exception:
            return False

    def _active_fence(self) -> dict[str, object] | None:
        if not self._gateway_can_accept():
            return None
        try:
            fence = self._runtime_fence_provider()
        except Exception:
            return None
        if not isinstance(fence, dict) or not fence:
            return None
        return fence

    @classmethod
    def _destination(
        cls,
        target: GatewaySystemNotificationTarget,
    ) -> _GatewayNotificationDestination:
        if not isinstance(target, GatewaySystemNotificationTarget):
            raise ValueError("target must implement GatewaySystemNotificationTarget")
        cls._require_text("target_id", target.target_id, _MAX_TARGET_ID_CHARS)
        metadata = target.metadata
        if not isinstance(metadata, Mapping):
            raise ValueError("target metadata must be a mapping")
        # Gateway 只解释它自己创建的少量投递字段，不把元数据写进 Outbox。
        platform = cls._require_text("platform", metadata.get("platform"), 128)
        chat_id = cls._require_text("chat_id", metadata.get("chat_id"), 1_024)
        thread_id = cls._optional_text("thread_id", metadata.get("thread_id"), 1_024)
        reply_to_message_id = cls._optional_text(
            "reply_to_message_id",
            metadata.get("reply_to_message_id"),
            1_024,
        )
        return _GatewayNotificationDestination(
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
        )

    @staticmethod
    def _matches_persisted_outbox(persisted: object, expected: dict) -> bool:
        if not isinstance(persisted, dict):
            return False
        for field_name in (
            "route_key",
            "source_message_id",
            "queue_message_id",
            "platform",
            "chat_id",
            "reply_to_message_id",
            "thread_id",
            "delivery_kind",
            "payloads",
        ):
            if persisted.get(field_name) != expected.get(field_name):
                return False
        return True

    @staticmethod
    def _require_text(field_name: str, value: object, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be a non-empty string")
        if len(value) > maximum:
            raise ValueError(f"{field_name} exceeds the supported length")
        return value

    @staticmethod
    def _optional_text(
        field_name: str,
        value: object,
        maximum: int,
    ) -> str | None:
        if value is None:
            return None
        return GatewaySystemNotificationPublisher._require_text(
            field_name,
            value,
            maximum,
        )


__all__ = [
    "GatewaySystemNotificationPublisher",
    "GatewaySystemNotificationReceipt",
    "GatewaySystemNotificationTarget",
    "SYSTEM_NOTIFICATION_DELIVERY_KIND",
]
