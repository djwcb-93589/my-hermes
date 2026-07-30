"""后台组件可选发布的运行状态契约，不包含持久化或调度。"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from collections.abc import Mapping
from typing import Protocol

from hermes.observability.contracts import (
    _optional_error_type,
    freeze_runtime_metadata,
)


logger = logging.getLogger(__name__)


class RuntimeComponentState(str, Enum):
    """组件主动发布的生命周期状态；读取层可在未来推导 stale。"""

    STARTING = "starting"
    RUNNING = "running"
    IDLE = "idle"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


def _identity(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _timestamp(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite number")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{field_name} must be a finite number")
    return normalized


@dataclass(frozen=True, slots=True)
class RuntimeComponentSnapshot:
    """一次组件状态快照；创建时复制并冻结所有安全 metadata。"""

    component_type: str
    component_id: str
    instance_id: str
    state: RuntimeComponentState
    started_at: float | None
    heartbeat_at: float
    stopped_at: float | None
    error_type: str | None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """拒绝不稳定身份、非法时间和不安全 metadata。"""
        for field_name in ("component_type", "component_id", "instance_id"):
            object.__setattr__(
                self,
                field_name,
                _identity(getattr(self, field_name), field_name),
            )
        if not isinstance(self.state, RuntimeComponentState):
            raise TypeError("state must be a RuntimeComponentState")
        for field_name in ("started_at", "stopped_at"):
            value = getattr(self, field_name)
            if value is not None:
                value = _timestamp(value, field_name)
            object.__setattr__(self, field_name, value)
        object.__setattr__(
            self,
            "heartbeat_at",
            _timestamp(self.heartbeat_at, "heartbeat_at"),
        )
        object.__setattr__(
            self,
            "error_type",
            _optional_error_type(self.error_type, "error_type"),
        )
        object.__setattr__(
            self,
            "metadata",
            freeze_runtime_metadata(self.metadata),
        )


class RuntimeStatusPublisher(Protocol):
    """运行时组件状态的同步发布端。"""

    def publish(self, snapshot: RuntimeComponentSnapshot) -> None:
        """接收一个已冻结状态快照。"""


class NullRuntimeStatusPublisher:
    """不保存状态、不创建线程的空发布端。"""

    __slots__ = ()

    def publish(self, snapshot: RuntimeComponentSnapshot) -> None:
        del snapshot


class RuntimeComponentReporter:
    """按需构造快照并以 best-effort 方式交给 Publisher。"""

    def __init__(
        self,
        *,
        component_type: str,
        component_id: str,
        instance_id: str,
        publisher: RuntimeStatusPublisher | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        self._component_type = _identity(component_type, "component_type")
        self._component_id = _identity(component_id, "component_id")
        self._instance_id = _identity(instance_id, "instance_id")
        self._publisher = (
            publisher if publisher is not None else NullRuntimeStatusPublisher()
        )
        self._metadata = freeze_runtime_metadata(
            {} if metadata is None else metadata
        )
        self._started_at: float | None = None
        self._state = RuntimeComponentState.STARTING

    def starting(self, *, at: float | None = None) -> RuntimeComponentSnapshot:
        """发布 starting 状态，并记录后续快照使用的启动时间。"""
        timestamp = _timestamp(time.time() if at is None else at, "started_at")
        self._started_at = timestamp
        return self._publish(RuntimeComponentState.STARTING, heartbeat_at=timestamp)

    def running(self, *, at: float | None = None) -> RuntimeComponentSnapshot:
        """发布 running 状态。"""
        return self._publish(RuntimeComponentState.RUNNING, heartbeat_at=at)

    def heartbeat(self, *, at: float | None = None) -> RuntimeComponentSnapshot:
        """以当前业务状态发布一次心跳，不创建定时任务。"""
        return self._publish(self._state, heartbeat_at=at)

    def idle(self, *, at: float | None = None) -> RuntimeComponentSnapshot:
        """发布 idle 状态。"""
        return self._publish(RuntimeComponentState.IDLE, heartbeat_at=at)

    def stopping(self, *, at: float | None = None) -> RuntimeComponentSnapshot:
        """发布 stopping 状态。"""
        return self._publish(RuntimeComponentState.STOPPING, heartbeat_at=at)

    def stopped(self, *, at: float | None = None) -> RuntimeComponentSnapshot:
        """发布 stopped 状态。"""
        return self._publish(
            RuntimeComponentState.STOPPED,
            heartbeat_at=at,
            stopped_at=at,
        )

    def failed(
        self,
        error_type: str,
        *,
        at: float | None = None,
    ) -> RuntimeComponentSnapshot:
        """发布 failed 状态，只保存错误类型而不保存异常对象。"""
        return self._publish(
            RuntimeComponentState.FAILED,
            heartbeat_at=at,
            error_type=error_type,
        )

    def _publish(
        self,
        state: RuntimeComponentState,
        *,
        heartbeat_at: float | None,
        stopped_at: float | None = None,
        error_type: str | None = None,
    ) -> RuntimeComponentSnapshot:
        now = _timestamp(
            time.time() if heartbeat_at is None else heartbeat_at,
            "heartbeat_at",
        )
        if self._started_at is None and state is not RuntimeComponentState.STARTING:
            self._started_at = now
        resolved_stopped_at = (
            now if state is RuntimeComponentState.STOPPED and stopped_at is None
            else stopped_at
        )
        snapshot = RuntimeComponentSnapshot(
            component_type=self._component_type,
            component_id=self._component_id,
            instance_id=self._instance_id,
            state=state,
            started_at=self._started_at,
            heartbeat_at=now,
            stopped_at=resolved_stopped_at,
            error_type=error_type,
            metadata=self._metadata,
        )
        self._state = state
        try:
            self._publisher.publish(snapshot)
        except Exception as exc:
            logger.warning(
                "Runtime status publisher failed: %s",
                type(exc).__name__,
            )
        return snapshot
