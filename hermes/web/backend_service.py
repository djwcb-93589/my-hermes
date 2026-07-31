"""Dashboard Backend Control 提交与状态读取应用服务。"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from hermes.backend_control import (
    BackendControlAction,
    BackendControlCreation,
    BackendControlInvalidRequest,
    BackendControlRepository,
    BackendControlRequest,
    BackendControlRequestNotFound,
    BackendControlUnavailable,
    BackendStatusReadRepository,
    BackendStatusSnapshot,
    BackendType,
    ConfigRevisionReader,
    validate_security_digest,
)


class BackendControlService:
    """只提交持久控制意图，不依赖 SQLite、Launcher 或 Gateway。"""

    __slots__ = ("_clock", "_repository")

    def __init__(
        self,
        repository: BackendControlRepository,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not callable(
            getattr(repository, "supervisor_online", None)
        ) or not callable(
            getattr(repository, "create_or_get_request", None)
        ):
            raise TypeError("backend control repository is invalid")
        if not callable(clock):
            raise TypeError("backend control clock is invalid")
        self._repository = repository
        self._clock = clock

    def submit_gateway_action(
        self,
        action: BackendControlAction,
        *,
        idempotency_key: str | None,
        actor_security_id: str | None,
    ) -> BackendControlCreation:
        """验证有限动作和幂等身份，并原子创建或取得原请求。"""
        if not isinstance(action, BackendControlAction):
            raise BackendControlInvalidRequest()
        try:
            actor = validate_security_digest(
                actor_security_id,
                "actor_security_id",
            )
        except ValueError as exc:
            raise BackendControlUnavailable() from exc
        key = _idempotency_key(idempotency_key)
        key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        fingerprint = hashlib.sha256(
            f"{BackendType.GATEWAY.value}:{action.value}".encode("ascii")
        ).hexdigest()
        created_at = _clock_value(self._clock)
        if not self._repository.supervisor_online(observed_at=created_at):
            raise BackendControlUnavailable("supervisor_unavailable")
        return self._repository.create_or_get_request(
            request_id=str(uuid.uuid4()),
            backend_type=BackendType.GATEWAY,
            action=action,
            actor_security_id=actor,
            idempotency_key_digest=key_digest,
            request_fingerprint=fingerprint,
            created_at=created_at,
        )


class BackendStatusReadService:
    """组合只读 Repository 与中立 revision reader，不执行控制。"""

    __slots__ = ("_clock", "_config_revision_reader", "_repository")

    def __init__(
        self,
        repository: BackendStatusReadRepository,
        config_revision_reader: ConfigRevisionReader,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not callable(getattr(repository, "read_status", None)) or not callable(
            getattr(repository, "get_request", None)
        ):
            raise TypeError("backend status repository is invalid")
        if not callable(getattr(config_revision_reader, "read_revision", None)):
            raise TypeError("config revision reader is invalid")
        if not callable(clock):
            raise TypeError("backend status clock is invalid")
        self._repository = repository
        self._config_revision_reader = config_revision_reader
        self._clock = clock

    def read_status(self) -> BackendStatusSnapshot:
        return self._repository.read_status(
            current_config_revision=self._config_revision_reader.read_revision(),
            observed_at=_clock_value(self._clock),
        )

    def get_request(self, request_id: str) -> BackendControlRequest:
        try:
            normalized = str(uuid.UUID(request_id))
        except (AttributeError, TypeError, ValueError) as exc:
            raise BackendControlRequestNotFound() from exc
        request = self._repository.get_request(normalized)
        if request is None:
            raise BackendControlRequestNotFound()
        return request


def _idempotency_key(value: object) -> str:
    if type(value) is not str:
        raise BackendControlInvalidRequest()
    normalized = value.strip()
    if not 8 <= len(normalized) <= 128:
        raise BackendControlInvalidRequest()
    return normalized


def _clock_value(clock: Callable[[], datetime]) -> datetime:
    try:
        value = clock()
    except Exception as exc:
        raise BackendControlUnavailable() from exc
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise BackendControlUnavailable()
    return value.astimezone(UTC)


__all__ = ["BackendControlService", "BackendStatusReadService"]
