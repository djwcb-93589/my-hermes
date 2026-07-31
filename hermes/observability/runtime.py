"""后台组件运行状态、心跳与只读投影的中立契约。"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from hermes.observability.contracts import (
    _optional_error_type,
    freeze_runtime_metadata,
)


logger = logging.getLogger(__name__)

DEFAULT_RUNTIME_HEARTBEAT_INTERVAL_SECONDS = 15.0
DEFAULT_RUNTIME_STALE_AFTER_SECONDS = 45.0
MAX_RUNTIME_HEARTBEAT_SECONDS = 86_400.0
MAX_RUNTIME_CLOCK_SKEW_SECONDS = 5.0
MAX_RUNTIME_PAGE_LIMIT = 100
MAX_RUNTIME_OFFSET = (1 << 63) - 1
_MAX_RUNTIME_REPOSITORY_FETCH_LIMIT = MAX_RUNTIME_PAGE_LIMIT + 1
_RUNTIME_IDENTITY_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,127}$"
)


class RuntimeComponentState(str, Enum):
    """组件主动发布的生命周期状态。"""

    STARTING = "starting"
    RUNNING = "running"
    IDLE = "idle"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class RuntimeComponentFreshness(str, Enum):
    """读取时推导的心跳新鲜度，不改写组件主动状态。"""

    FRESH = "fresh"
    STALE = "stale"
    TERMINAL = "terminal"
    CLOCK_SKEWED = "clock_skewed"


class RuntimeComponentEffectiveStatus(str, Enum):
    """Dashboard 可展示的有限有效状态。"""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STALE = "stale"
    STOPPED = "stopped"
    FAILED = "failed"


class RuntimeLifecycleTransitionError(ValueError):
    """同一 Runtime 实例发生不允许的生命周期转换。"""


class RuntimeStatusRepositoryError(RuntimeError):
    """Runtime 当前状态只读仓储的稳定错误边界。"""

    def __init__(self, reason_code: str) -> None:
        if reason_code not in {
            "database_busy",
            "database_unavailable",
            "record_invalid",
            "schema_incompatible",
        }:
            raise ValueError("runtime repository reason_code is invalid")
        self.reason_code = reason_code
        super().__init__(reason_code)


class RuntimeStatusRepositoryUnavailable(RuntimeStatusRepositoryError):
    """数据库无法在当前请求边界内完成只读访问。"""

    def __init__(self, reason_code: str) -> None:
        if reason_code not in {"database_busy", "database_unavailable"}:
            raise ValueError("runtime unavailable reason_code is invalid")
        super().__init__(reason_code)


class RuntimeStatusRecordInvalid(RuntimeStatusRepositoryError):
    """Schema 不兼容或当前快照记录损坏。"""

    def __init__(self, reason_code: str = "record_invalid") -> None:
        if reason_code not in {"record_invalid", "schema_incompatible"}:
            raise ValueError("runtime record reason_code is invalid")
        super().__init__(reason_code)


def _identity(value: object, field_name: str) -> str:
    """只接收稳定、紧凑且不含路径或正文的低基数身份。"""
    if type(value) is not str:
        raise ValueError(f"{field_name} must be a compact runtime identity")
    normalized = value.strip()
    if (
        not normalized
        or not _RUNTIME_IDENTITY_RE.fullmatch(normalized)
    ):
        raise ValueError(f"{field_name} must be a compact runtime identity")
    return normalized


def _timestamp(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite non-negative number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{field_name} must be a finite non-negative number")
    return normalized


def _positive_policy_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite positive number")
    normalized = float(value)
    if (
        not math.isfinite(normalized)
        or normalized <= 0
        or normalized > MAX_RUNTIME_HEARTBEAT_SECONDS
    ):
        raise ValueError(f"{field_name} is outside the runtime policy boundary")
    return normalized


def _runtime_state(
    value: RuntimeComponentState | str,
    field_name: str = "reported_state",
) -> RuntimeComponentState:
    if isinstance(value, RuntimeComponentState):
        return value
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a RuntimeComponentState")
    try:
        return RuntimeComponentState(value)
    except ValueError:
        raise ValueError(f"{field_name} is invalid") from None


def _nonnegative_integer(value: object, field_name: str) -> int:
    if (
        type(value) is not int
        or value < 0
        or value > MAX_RUNTIME_OFFSET
    ):
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _page_limit(value: object, *, repository: bool = False) -> int:
    maximum = (
        _MAX_RUNTIME_REPOSITORY_FETCH_LIMIT
        if repository
        else MAX_RUNTIME_PAGE_LIMIT
    )
    if type(value) is not int or value < 1 or value > maximum:
        raise ValueError("limit is outside the runtime read boundary")
    return value


@dataclass(frozen=True, slots=True)
class RuntimeHeartbeatPolicy:
    """由组件显式声明并随 Snapshot 持久化的心跳策略。"""

    heartbeat_interval_seconds: float = (
        DEFAULT_RUNTIME_HEARTBEAT_INTERVAL_SECONDS
    )
    stale_after_seconds: float = DEFAULT_RUNTIME_STALE_AFTER_SECONDS

    def __post_init__(self) -> None:
        interval = _positive_policy_number(
            self.heartbeat_interval_seconds,
            "heartbeat_interval_seconds",
        )
        stale_after = _positive_policy_number(
            self.stale_after_seconds,
            "stale_after_seconds",
        )
        if stale_after <= interval:
            raise ValueError(
                "stale_after_seconds must exceed heartbeat_interval_seconds"
            )
        object.__setattr__(self, "heartbeat_interval_seconds", interval)
        object.__setattr__(self, "stale_after_seconds", stale_after)


DEFAULT_RUNTIME_HEARTBEAT_POLICY = RuntimeHeartbeatPolicy()


_RUNTIME_TRANSITIONS: Mapping[
    RuntimeComponentState,
    frozenset[RuntimeComponentState],
] = {
    RuntimeComponentState.STARTING: frozenset({
        RuntimeComponentState.STARTING,
        RuntimeComponentState.RUNNING,
        RuntimeComponentState.IDLE,
        RuntimeComponentState.STOPPING,
        RuntimeComponentState.STOPPED,
        RuntimeComponentState.FAILED,
    }),
    RuntimeComponentState.RUNNING: frozenset({
        RuntimeComponentState.RUNNING,
        RuntimeComponentState.IDLE,
        RuntimeComponentState.STOPPING,
        RuntimeComponentState.STOPPED,
        RuntimeComponentState.FAILED,
    }),
    RuntimeComponentState.IDLE: frozenset({
        RuntimeComponentState.IDLE,
        RuntimeComponentState.RUNNING,
        RuntimeComponentState.STOPPING,
        RuntimeComponentState.STOPPED,
        RuntimeComponentState.FAILED,
    }),
    RuntimeComponentState.STOPPING: frozenset({
        RuntimeComponentState.STOPPING,
        RuntimeComponentState.STOPPED,
        RuntimeComponentState.FAILED,
    }),
    RuntimeComponentState.STOPPED: frozenset({
        RuntimeComponentState.STOPPED,
    }),
    RuntimeComponentState.FAILED: frozenset({
        RuntimeComponentState.FAILED,
    }),
}


def validate_runtime_transition(
    previous: RuntimeComponentState,
    current: RuntimeComponentState,
) -> None:
    """校验同一实例转换；终态只能保持原终态，不能复活或互换。"""
    if not isinstance(previous, RuntimeComponentState):
        raise TypeError("previous must be a RuntimeComponentState")
    if not isinstance(current, RuntimeComponentState):
        raise TypeError("current must be a RuntimeComponentState")
    if current not in _RUNTIME_TRANSITIONS[previous]:
        raise RuntimeLifecycleTransitionError(
            f"runtime transition is not allowed: "
            f"{previous.value} -> {current.value}"
        )


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
    heartbeat_interval_seconds: float = (
        DEFAULT_RUNTIME_HEARTBEAT_INTERVAL_SECONDS
    )
    stale_after_seconds: float = DEFAULT_RUNTIME_STALE_AFTER_SECONDS

    def __post_init__(self) -> None:
        """拒绝不稳定身份、非法时间、状态形状和不安全 metadata。"""
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
        heartbeat_at = _timestamp(self.heartbeat_at, "heartbeat_at")
        object.__setattr__(self, "heartbeat_at", heartbeat_at)
        error_type = _optional_error_type(self.error_type, "error_type")
        object.__setattr__(self, "error_type", error_type)
        policy = RuntimeHeartbeatPolicy(
            heartbeat_interval_seconds=self.heartbeat_interval_seconds,
            stale_after_seconds=self.stale_after_seconds,
        )
        object.__setattr__(
            self,
            "heartbeat_interval_seconds",
            policy.heartbeat_interval_seconds,
        )
        object.__setattr__(
            self,
            "stale_after_seconds",
            policy.stale_after_seconds,
        )
        object.__setattr__(
            self,
            "metadata",
            freeze_runtime_metadata(self.metadata),
        )
        if self.started_at is not None and self.started_at > heartbeat_at:
            raise ValueError("started_at must not exceed heartbeat_at")
        if self.state is RuntimeComponentState.STOPPED:
            if self.stopped_at is None:
                raise ValueError("stopped snapshot must include stopped_at")
        elif self.stopped_at is not None:
            raise ValueError("non-stopped snapshot must not include stopped_at")
        if self.stopped_at is not None:
            if (
                self.started_at is not None
                and self.stopped_at < self.started_at
            ):
                raise ValueError("stopped_at must not precede started_at")
            if self.stopped_at > heartbeat_at:
                raise ValueError("stopped_at must not exceed heartbeat_at")
        if self.state is not RuntimeComponentState.FAILED and error_type is not None:
            raise ValueError("error_type is only allowed for failed state")

    @property
    def reported_state(self) -> RuntimeComponentState:
        """为读取领域提供明确名称，同时保留原有 ``state`` 字段。"""
        return self.state


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
        heartbeat_policy: RuntimeHeartbeatPolicy | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._component_type = _identity(component_type, "component_type")
        self._component_id = _identity(component_id, "component_id")
        self._instance_id = _identity(instance_id, "instance_id")
        self._publisher = (
            publisher if publisher is not None else NullRuntimeStatusPublisher()
        )
        if not callable(getattr(self._publisher, "publish", None)):
            raise TypeError("publisher must implement RuntimeStatusPublisher")
        self._metadata = freeze_runtime_metadata(
            {} if metadata is None else metadata
        )
        if heartbeat_policy is None:
            heartbeat_policy = DEFAULT_RUNTIME_HEARTBEAT_POLICY
        if not isinstance(heartbeat_policy, RuntimeHeartbeatPolicy):
            raise TypeError("heartbeat_policy must be a RuntimeHeartbeatPolicy")
        self._heartbeat_policy = heartbeat_policy
        self._clock = time.time if clock is None else clock
        if not callable(self._clock):
            raise TypeError("clock must be callable")
        self._started_at: float | None = None
        self._state = RuntimeComponentState.STARTING

    @property
    def component_type(self) -> str:
        return self._component_type

    @property
    def current_state(self) -> RuntimeComponentState:
        return self._state

    @property
    def heartbeat_policy(self) -> RuntimeHeartbeatPolicy:
        return self._heartbeat_policy

    def starting(
        self,
        *,
        at: float | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> RuntimeComponentSnapshot:
        """发布 starting，并只在首次发布时固定启动代际。"""
        timestamp = self._now(at, "started_at")
        if self._started_at is None:
            self._started_at = timestamp
        return self._publish(
            RuntimeComponentState.STARTING,
            heartbeat_at=timestamp,
            metadata=metadata,
        )

    def running(
        self,
        *,
        at: float | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> RuntimeComponentSnapshot:
        """发布 running 状态。"""
        return self._publish(
            RuntimeComponentState.RUNNING,
            heartbeat_at=at,
            metadata=metadata,
        )

    def heartbeat(
        self,
        *,
        at: float | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> RuntimeComponentSnapshot:
        """以当前状态发布心跳；终态实例不允许继续发送心跳。"""
        if self._state in {
            RuntimeComponentState.STOPPED,
            RuntimeComponentState.FAILED,
        }:
            raise RuntimeLifecycleTransitionError(
                "terminal runtime component cannot publish heartbeat"
            )
        return self._publish(
            self._state,
            heartbeat_at=at,
            metadata=metadata,
        )

    def idle(
        self,
        *,
        at: float | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> RuntimeComponentSnapshot:
        """发布 idle 状态。"""
        return self._publish(
            RuntimeComponentState.IDLE,
            heartbeat_at=at,
            metadata=metadata,
        )

    def stopping(
        self,
        *,
        at: float | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> RuntimeComponentSnapshot:
        """发布 stopping 状态。"""
        return self._publish(
            RuntimeComponentState.STOPPING,
            heartbeat_at=at,
            metadata=metadata,
        )

    def stopped(
        self,
        *,
        at: float | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> RuntimeComponentSnapshot:
        """发布 stopped 状态。"""
        timestamp = self._now(at, "heartbeat_at")
        return self._publish(
            RuntimeComponentState.STOPPED,
            heartbeat_at=timestamp,
            stopped_at=timestamp,
            metadata=metadata,
        )

    def failed(
        self,
        error_type: str,
        *,
        at: float | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> RuntimeComponentSnapshot:
        """发布 failed，只保存错误类型而不保存异常正文。"""
        return self._publish(
            RuntimeComponentState.FAILED,
            heartbeat_at=at,
            error_type=error_type,
            metadata=metadata,
        )

    def _now(self, at: float | None, field_name: str) -> float:
        return _timestamp(self._clock() if at is None else at, field_name)

    def _resolved_metadata(
        self,
        metadata: Mapping[str, object] | None,
    ) -> Mapping[str, object]:
        if metadata is None:
            return self._metadata
        try:
            dynamic = freeze_runtime_metadata(metadata)
            combined = dict(self._metadata)
            combined.update(dynamic)
            return freeze_runtime_metadata(combined)
        except Exception as exc:
            logger.warning(
                "Runtime component reporting failed: "
                "component_type=%s publish_stage=metadata "
                "exception_type=%s",
                self._component_type,
                type(exc).__name__,
            )
            return self._metadata

    def _publish(
        self,
        state: RuntimeComponentState,
        *,
        heartbeat_at: float | None,
        stopped_at: float | None = None,
        error_type: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> RuntimeComponentSnapshot:
        validate_runtime_transition(self._state, state)
        now = self._now(heartbeat_at, "heartbeat_at")
        if self._started_at is None:
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
            metadata=self._resolved_metadata(metadata),
            heartbeat_interval_seconds=(
                self._heartbeat_policy.heartbeat_interval_seconds
            ),
            stale_after_seconds=self._heartbeat_policy.stale_after_seconds,
        )
        self._state = state
        try:
            self._publisher.publish(snapshot)
        except Exception as exc:
            logger.warning(
                "Runtime component reporting failed: "
                "component_type=%s publish_stage=publish "
                "exception_type=%s",
                self._component_type,
                type(exc).__name__,
            )
        return snapshot


@dataclass(frozen=True, slots=True)
class RuntimeProbeResult:
    """一次安全 liveness probe 的有限结果。"""

    state: RuntimeComponentState
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.state, RuntimeComponentState):
            raise TypeError("runtime probe state must be a RuntimeComponentState")
        if self.state not in {
            RuntimeComponentState.RUNNING,
            RuntimeComponentState.IDLE,
        }:
            raise ValueError("runtime probe state must be running or idle")
        object.__setattr__(
            self,
            "metadata",
            freeze_runtime_metadata(self.metadata),
        )


class RuntimeComponentProbe(Protocol):
    """组件拥有者提供的轻量 liveness probe。"""

    def __call__(
        self,
    ) -> RuntimeProbeResult | Awaitable[RuntimeProbeResult]:
        """返回有限状态与安全摘要，不暴露组件对象。"""


class AsyncRuntimeHeartbeat:
    """在所属事件循环内运行、不会延长组件生命周期的心跳任务。"""

    def __init__(
        self,
        reporter: RuntimeComponentReporter,
        probe: RuntimeComponentProbe,
        *,
        policy: RuntimeHeartbeatPolicy | None = None,
    ) -> None:
        if not isinstance(reporter, RuntimeComponentReporter):
            raise TypeError("reporter must be a RuntimeComponentReporter")
        if not callable(probe):
            raise TypeError("probe must be callable")
        resolved_policy = reporter.heartbeat_policy if policy is None else policy
        if not isinstance(resolved_policy, RuntimeHeartbeatPolicy):
            raise TypeError("policy must be a RuntimeHeartbeatPolicy")
        if resolved_policy != reporter.heartbeat_policy:
            raise ValueError("heartbeat policy must match reporter policy")
        self._reporter = reporter
        self._probe = probe
        self._policy = resolved_policy
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """在当前运行事件循环创建唯一心跳 Task。"""
        if self._stopped:
            raise RuntimeError("runtime heartbeat has been stopped")
        if self.running:
            return
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._run())

    async def stop(self) -> None:
        """可靠取消并等待 Task 退出，不发布伪造的停止状态。"""
        self._stopped = True
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        try:
            while True:
                try:
                    result = self._probe()
                    if inspect.isawaitable(result):
                        result = await result
                    if not isinstance(result, RuntimeProbeResult):
                        raise TypeError(
                            "runtime probe must return RuntimeProbeResult"
                        )
                    if result.state is RuntimeComponentState.RUNNING:
                        self._reporter.running(metadata=result.metadata)
                    else:
                        self._reporter.idle(metadata=result.metadata)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "Runtime component reporting failed: "
                        "component_type=%s publish_stage=probe "
                        "exception_type=%s",
                        self._reporter.component_type,
                        type(exc).__name__,
                    )
                    return
                await asyncio.sleep(
                    self._policy.heartbeat_interval_seconds
                )
        except asyncio.CancelledError:
            raise


@dataclass(frozen=True, slots=True)
class RuntimeComponentRecord:
    """持久化层返回的当前逻辑组件安全投影。"""

    component_type: str
    component_id: str
    instance_id: str
    reported_state: RuntimeComponentState | str
    started_at: float | None
    heartbeat_at: float
    stopped_at: float | None
    error_type: str | None
    heartbeat_interval_seconds: float
    stale_after_seconds: float
    metadata: Mapping[str, object] = field(default_factory=dict)
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        state = _runtime_state(self.reported_state)
        snapshot = RuntimeComponentSnapshot(
            component_type=self.component_type,
            component_id=self.component_id,
            instance_id=self.instance_id,
            state=state,
            started_at=self.started_at,
            heartbeat_at=self.heartbeat_at,
            stopped_at=self.stopped_at,
            error_type=self.error_type,
            metadata=self.metadata,
            heartbeat_interval_seconds=self.heartbeat_interval_seconds,
            stale_after_seconds=self.stale_after_seconds,
        )
        for field_name in (
            "component_type",
            "component_id",
            "instance_id",
            "started_at",
            "heartbeat_at",
            "stopped_at",
            "error_type",
            "heartbeat_interval_seconds",
            "stale_after_seconds",
            "metadata",
        ):
            object.__setattr__(
                self,
                field_name,
                getattr(snapshot, field_name),
            )
        object.__setattr__(self, "reported_state", state)
        object.__setattr__(
            self,
            "updated_at",
            _timestamp(self.updated_at, "updated_at"),
        )


@dataclass(frozen=True, slots=True)
class RuntimeComponentStatusView:
    """在单一 observed_at 上推导出的组件有效状态。"""

    component_type: str
    component_id: str
    instance_id: str
    reported_state: RuntimeComponentState
    freshness: RuntimeComponentFreshness
    effective_status: RuntimeComponentEffectiveStatus
    started_at: float | None
    heartbeat_at: float
    heartbeat_age_seconds: float
    heartbeat_interval_seconds: float
    stale_after_seconds: float
    stopped_at: float | None
    error_type: str | None
    metadata: Mapping[str, object]
    updated_at: float
    is_stale: bool

    def __post_init__(self) -> None:
        record = RuntimeComponentRecord(
            component_type=self.component_type,
            component_id=self.component_id,
            instance_id=self.instance_id,
            reported_state=self.reported_state,
            started_at=self.started_at,
            heartbeat_at=self.heartbeat_at,
            stopped_at=self.stopped_at,
            error_type=self.error_type,
            heartbeat_interval_seconds=self.heartbeat_interval_seconds,
            stale_after_seconds=self.stale_after_seconds,
            metadata=self.metadata,
            updated_at=self.updated_at,
        )
        for field_name in (
            "component_type",
            "component_id",
            "instance_id",
            "reported_state",
            "started_at",
            "heartbeat_at",
            "heartbeat_interval_seconds",
            "stale_after_seconds",
            "stopped_at",
            "error_type",
            "metadata",
            "updated_at",
        ):
            object.__setattr__(self, field_name, getattr(record, field_name))
        if not isinstance(self.freshness, RuntimeComponentFreshness):
            raise TypeError("freshness must be a RuntimeComponentFreshness")
        if not isinstance(
            self.effective_status,
            RuntimeComponentEffectiveStatus,
        ):
            raise TypeError(
                "effective_status must be a RuntimeComponentEffectiveStatus"
            )
        object.__setattr__(
            self,
            "heartbeat_age_seconds",
            _timestamp(
                self.heartbeat_age_seconds,
                "heartbeat_age_seconds",
            ),
        )
        if type(self.is_stale) is not bool:
            raise TypeError("is_stale must be a boolean")
        if self.is_stale is not (
            self.freshness is RuntimeComponentFreshness.STALE
        ):
            raise ValueError("is_stale is inconsistent with freshness")
        terminal = self.reported_state in {
            RuntimeComponentState.STOPPED,
            RuntimeComponentState.FAILED,
        }
        if terminal:
            expected_status = (
                RuntimeComponentEffectiveStatus.STOPPED
                if self.reported_state is RuntimeComponentState.STOPPED
                else RuntimeComponentEffectiveStatus.FAILED
            )
            if (
                self.freshness is not RuntimeComponentFreshness.TERMINAL
                or self.effective_status is not expected_status
                or self.is_stale
            ):
                raise ValueError(
                    "terminal runtime status projection is inconsistent"
                )
        elif self.freshness is RuntimeComponentFreshness.TERMINAL:
            raise ValueError(
                "active runtime status must not have terminal freshness"
            )
        elif self.freshness is RuntimeComponentFreshness.STALE:
            if (
                self.effective_status
                is not RuntimeComponentEffectiveStatus.STALE
                or self.heartbeat_age_seconds < self.stale_after_seconds
            ):
                raise ValueError("stale runtime projection is inconsistent")
        elif self.freshness is RuntimeComponentFreshness.CLOCK_SKEWED:
            if (
                self.effective_status
                is not RuntimeComponentEffectiveStatus.DEGRADED
                or self.heartbeat_age_seconds != 0
            ):
                raise ValueError(
                    "clock-skewed runtime projection is inconsistent"
                )
        else:
            expected_status = (
                RuntimeComponentEffectiveStatus.HEALTHY
                if self.reported_state
                in {
                    RuntimeComponentState.RUNNING,
                    RuntimeComponentState.IDLE,
                }
                else RuntimeComponentEffectiveStatus.DEGRADED
            )
            if (
                self.effective_status is not expected_status
                or self.heartbeat_age_seconds >= self.stale_after_seconds
            ):
                raise ValueError("fresh runtime projection is inconsistent")


@dataclass(frozen=True, slots=True)
class RuntimeComponentPage:
    """一页在同一观察时刻推导的 Runtime 当前状态。"""

    observed_at: float
    items: tuple[RuntimeComponentStatusView, ...]
    limit: int
    offset: int
    has_more: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_at",
            _timestamp(self.observed_at, "observed_at"),
        )
        if not isinstance(self.items, tuple) or not all(
            isinstance(item, RuntimeComponentStatusView)
            for item in self.items
        ):
            raise TypeError(
                "items must be a tuple of RuntimeComponentStatusView"
            )
        object.__setattr__(self, "limit", _page_limit(self.limit))
        object.__setattr__(
            self,
            "offset",
            _nonnegative_integer(self.offset, "offset"),
        )
        if type(self.has_more) is not bool:
            raise TypeError("has_more must be a boolean")
        if len(self.items) > self.limit:
            raise ValueError("runtime page exceeds limit")


class RuntimeStatusReadRepository(Protocol):
    """只读取当前 Runtime Snapshot 的中立仓储边界。"""

    def list_components(
        self,
        *,
        component_type: str | None = None,
        reported_state: RuntimeComponentState | None = None,
        limit: int,
        offset: int,
    ) -> tuple[RuntimeComponentRecord, ...]:
        """按组件类型和身份固定升序读取，内部最多允许 limit=101。"""

    def get_component(
        self,
        component_type: str,
        component_id: str,
    ) -> RuntimeComponentRecord | None:
        """按逻辑组件主键读取当前有效实例。"""


def derive_runtime_component_status(
    record: RuntimeComponentRecord,
    *,
    observed_at: float,
    clock_skew_tolerance_seconds: float = MAX_RUNTIME_CLOCK_SKEW_SECONDS,
) -> RuntimeComponentStatusView:
    """在单一时钟值上推导 freshness 与 effective status。"""
    if not isinstance(record, RuntimeComponentRecord):
        raise TypeError("record must be a RuntimeComponentRecord")
    now = _timestamp(observed_at, "observed_at")
    tolerance = _timestamp(
        clock_skew_tolerance_seconds,
        "clock_skew_tolerance_seconds",
    )
    if tolerance > MAX_RUNTIME_CLOCK_SKEW_SECONDS:
        raise ValueError(
            "clock_skew_tolerance_seconds exceeds the fixed boundary"
        )

    state = record.reported_state
    terminal = state in {
        RuntimeComponentState.STOPPED,
        RuntimeComponentState.FAILED,
    }
    heartbeat_delta = now - record.heartbeat_at
    heartbeat_age = max(0.0, heartbeat_delta)
    if not math.isfinite(heartbeat_age):
        raise ValueError("heartbeat age must be finite")

    if terminal:
        freshness = RuntimeComponentFreshness.TERMINAL
        effective_status = (
            RuntimeComponentEffectiveStatus.STOPPED
            if state is RuntimeComponentState.STOPPED
            else RuntimeComponentEffectiveStatus.FAILED
        )
    elif heartbeat_delta < -tolerance:
        freshness = RuntimeComponentFreshness.CLOCK_SKEWED
        effective_status = RuntimeComponentEffectiveStatus.DEGRADED
    elif heartbeat_age >= record.stale_after_seconds:
        freshness = RuntimeComponentFreshness.STALE
        effective_status = RuntimeComponentEffectiveStatus.STALE
    else:
        freshness = RuntimeComponentFreshness.FRESH
        effective_status = (
            RuntimeComponentEffectiveStatus.HEALTHY
            if state in {
                RuntimeComponentState.RUNNING,
                RuntimeComponentState.IDLE,
            }
            else RuntimeComponentEffectiveStatus.DEGRADED
        )

    return RuntimeComponentStatusView(
        component_type=record.component_type,
        component_id=record.component_id,
        instance_id=record.instance_id,
        reported_state=state,
        freshness=freshness,
        effective_status=effective_status,
        started_at=record.started_at,
        heartbeat_at=record.heartbeat_at,
        heartbeat_age_seconds=heartbeat_age,
        heartbeat_interval_seconds=record.heartbeat_interval_seconds,
        stale_after_seconds=record.stale_after_seconds,
        stopped_at=record.stopped_at,
        error_type=record.error_type,
        metadata=record.metadata,
        updated_at=record.updated_at,
        is_stale=freshness is RuntimeComponentFreshness.STALE,
    )


def validate_runtime_repository_limit(limit: object) -> int:
    """供 SQLite 适配器共享内部 limit+1 边界，不暴露自由 SQL limit。"""
    return _page_limit(limit, repository=True)


def validate_runtime_identity(value: object, field_name: str) -> str:
    """供中立适配器复用 Runtime 身份边界。"""
    if type(field_name) is not str or not field_name:
        raise ValueError("field_name must be a non-empty string")
    return _identity(value, field_name)


__all__ = [
    "AsyncRuntimeHeartbeat",
    "DEFAULT_RUNTIME_HEARTBEAT_INTERVAL_SECONDS",
    "DEFAULT_RUNTIME_HEARTBEAT_POLICY",
    "DEFAULT_RUNTIME_STALE_AFTER_SECONDS",
    "MAX_RUNTIME_CLOCK_SKEW_SECONDS",
    "MAX_RUNTIME_HEARTBEAT_SECONDS",
    "MAX_RUNTIME_OFFSET",
    "MAX_RUNTIME_PAGE_LIMIT",
    "NullRuntimeStatusPublisher",
    "RuntimeComponentEffectiveStatus",
    "RuntimeComponentFreshness",
    "RuntimeComponentPage",
    "RuntimeComponentProbe",
    "RuntimeComponentRecord",
    "RuntimeComponentReporter",
    "RuntimeComponentSnapshot",
    "RuntimeComponentState",
    "RuntimeComponentStatusView",
    "RuntimeHeartbeatPolicy",
    "RuntimeLifecycleTransitionError",
    "RuntimeProbeResult",
    "RuntimeStatusPublisher",
    "RuntimeStatusReadRepository",
    "RuntimeStatusRecordInvalid",
    "RuntimeStatusRepositoryError",
    "RuntimeStatusRepositoryUnavailable",
    "derive_runtime_component_status",
    "validate_runtime_identity",
    "validate_runtime_repository_limit",
    "validate_runtime_transition",
]
