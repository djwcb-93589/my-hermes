"""基于现有 runtime lease 基础设施的 Supervisor 独占租约。"""

from __future__ import annotations

import math
import sqlite3
import time
import uuid
from collections.abc import Callable

from hermes.backend_control import (
    SUPERVISOR_RUNTIME_LEASE_NAME,
    SupervisorFence,
    SupervisorLeaseLost,
)
from hermes.persistence.gateway import (
    acquire_gateway_runtime_lease,
    gateway_runtime_lease_is_valid,
    release_gateway_runtime_lease,
    renew_gateway_runtime_lease,
)


SUPERVISOR_LEASE_TTL_SECONDS = 15.0
SUPERVISOR_LEASE_HEARTBEAT_SECONDS = 5.0


class SupervisorLeaseUnavailable(RuntimeError):
    """当前 profile 已有其他有效 Supervisor。"""


class SQLiteSupervisorLease:
    """复用通用 runtime lease 表并提供稳定 fencing 身份。"""

    __slots__ = (
        "_connection",
        "_heartbeat_seconds",
        "_instance_id",
        "_last_renewed",
        "_lease_epoch",
        "_monotonic",
        "_ttl_seconds",
    )

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        ttl_seconds: float = SUPERVISOR_LEASE_TTL_SECONDS,
        heartbeat_seconds: float = SUPERVISOR_LEASE_HEARTBEAT_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("supervisor lease connection is invalid")
        if (
            not math.isfinite(float(ttl_seconds))
            or not math.isfinite(float(heartbeat_seconds))
            or heartbeat_seconds <= 0
            or ttl_seconds <= heartbeat_seconds * 2
        ):
            raise ValueError("supervisor lease timing is invalid")
        if not callable(monotonic):
            raise TypeError("supervisor monotonic clock is invalid")
        self._connection = connection
        self._ttl_seconds = float(ttl_seconds)
        self._heartbeat_seconds = float(heartbeat_seconds)
        self._monotonic = monotonic
        self._instance_id = str(uuid.uuid4())
        self._lease_epoch: int | None = None
        self._last_renewed = 0.0

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def fence(self) -> SupervisorFence:
        if self._lease_epoch is None:
            raise SupervisorLeaseLost()
        return SupervisorFence(self._instance_id, self._lease_epoch)

    def acquire(self) -> bool:
        acquired = acquire_gateway_runtime_lease(
            self._connection,
            SUPERVISOR_RUNTIME_LEASE_NAME,
            self._instance_id,
            self._ttl_seconds,
        )
        if acquired is None:
            return False
        self._lease_epoch = int(acquired["lease_epoch"])
        self._last_renewed = self._monotonic()
        return True

    def require_valid(
        self,
        *,
        force_renew: bool = False,
    ) -> SupervisorFence:
        if self._lease_epoch is None:
            raise SupervisorLeaseLost()
        now = self._monotonic()
        if force_renew or now - self._last_renewed >= self._heartbeat_seconds:
            renewed = renew_gateway_runtime_lease(
                self._connection,
                SUPERVISOR_RUNTIME_LEASE_NAME,
                self._instance_id,
                self._lease_epoch,
                self._ttl_seconds,
            )
            if not renewed:
                self._lease_epoch = None
                raise SupervisorLeaseLost()
            self._last_renewed = now
        elif not gateway_runtime_lease_is_valid(
            self._connection,
            SUPERVISOR_RUNTIME_LEASE_NAME,
            self._instance_id,
            self._lease_epoch,
        ):
            self._lease_epoch = None
            raise SupervisorLeaseLost()
        return self.fence

    def release(self) -> None:
        lease_epoch = self._lease_epoch
        self._lease_epoch = None
        if lease_epoch is None:
            return
        try:
            release_gateway_runtime_lease(
                self._connection,
                SUPERVISOR_RUNTIME_LEASE_NAME,
                self._instance_id,
                lease_epoch,
            )
        except Exception:
            # TTL 会保证租约最终失效；释放失败不能触发任何进程控制。
            return


__all__ = [
    "SQLiteSupervisorLease",
    "SupervisorLeaseUnavailable",
]
