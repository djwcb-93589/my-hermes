"""将公共异步 Hook 安全桥接给同步运行路径。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from threading import RLock

from hermes.hooks.async_registry import (
    AsyncHookRegistry,
    _AsyncControlSnapshot,
)
from hermes.hooks.contracts import (
    HookDispatchResult,
    HookEvent,
    HookInvocationResult,
    HookName,
    HookRegistration,
    HookRegistrationError,
)
from hermes.hooks.controls import (
    HookControlDispatchResult,
    build_control_dispatch_result,
    control_failure_reason,
)
from hermes.hooks.events import HookEventName
from hermes.hooks.registry import SyncHookRegistry, _normalize_timeout


logger = logging.getLogger(__name__)

_CONTROL_EVENTS = (
    HookEventName.PRE_LLM_CALL.value,
    HookEventName.PRE_TOOL_CALL.value,
)
_OBSERVATION_EVENTS = frozenset({
    HookEventName.POST_LLM_CALL.value,
    HookEventName.POST_TOOL_CALL.value,
    HookEventName.RUN_END.value,
})
_BRIDGE_DEFAULT_TIMEOUT_SECONDS = 5.0
_BRIDGE_OVERHEAD_SECONDS = 1.0
DEFAULT_BRIDGE_TOTAL_TIMEOUT_SECONDS = 15.0


def _observation_failure_result(event: HookEvent) -> HookDispatchResult:
    """构造不会改变同步运行业务结果的观察桥接失败摘要。"""
    return HookDispatchResult(
        event=event,
        results=(
            HookInvocationResult(
                hook_id="observation_bridge",
                success=False,
                error_type="HookObservationBridgeError",
                error_message="hook observation bridge failed",
            ),
        ),
    )


def _consume_observation_future(
    future: concurrent.futures.Future[HookDispatchResult],
) -> None:
    """消费已脱离等待的观察分发结果，不记录异常正文。"""
    def consume(
        completed: concurrent.futures.Future[HookDispatchResult],
    ) -> None:
        try:
            completed.result()
        except Exception:
            return

    future.add_done_callback(consume)


def _forward_observation_event(
    event: HookEvent,
    *,
    registry: AsyncHookRegistry | None,
    event_loop: asyncio.AbstractEventLoop | None,
    closed: bool,
    timeout_seconds: float,
) -> HookDispatchResult:
    """把一个同步观察事件安全提交到指定 Async Registry。"""
    try:
        if asyncio.get_running_loop() is event_loop:
            return _observation_failure_result(event)
    except RuntimeError:
        pass
    if (
        closed
        or registry is None
        or event_loop is None
        or event_loop.is_closed()
        or not event_loop.is_running()
    ):
        return _observation_failure_result(event)

    coroutine = registry.emit(event)
    try:
        future = asyncio.run_coroutine_threadsafe(coroutine, event_loop)
    except Exception as exc:
        coroutine.close()
        logger.warning(
            "Observation Hook bridge submission failed: "
            "event=%s exception=%s",
            event.name,
            type(exc).__name__,
        )
        return _observation_failure_result(event)
    try:
        return future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError:
        future.cancel()
        _consume_observation_future(future)
        logger.warning(
            "Observation Hook bridge timed out: event=%s",
            event.name,
        )
        return _observation_failure_result(event)
    except Exception as exc:
        _consume_observation_future(future)
        logger.warning(
            "Observation Hook bridge dispatch failed: "
            "event=%s exception=%s",
            event.name,
            type(exc).__name__,
        )
        return _observation_failure_result(event)


class SyncObservationBridge(SyncHookRegistry):
    """只把同步运行产生的观察事件转发到公共 Async Registry。"""

    def __init__(
        self,
        registry: AsyncHookRegistry,
        event_loop: asyncio.AbstractEventLoop,
        *,
        bridge_total_timeout_seconds: float = (
            DEFAULT_BRIDGE_TOTAL_TIMEOUT_SECONDS
        ),
    ) -> None:
        super().__init__()
        if not isinstance(registry, AsyncHookRegistry):
            raise TypeError("registry must be an AsyncHookRegistry")
        if event_loop.is_closed() or not event_loop.is_running():
            raise RuntimeError("event_loop must be running")
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError(
                "SyncObservationBridge must be created on event_loop"
            ) from exc
        if current_loop is not event_loop:
            raise RuntimeError(
                "SyncObservationBridge must be created on event_loop"
            )
        normalized_total_timeout = _normalize_timeout(
            bridge_total_timeout_seconds
        )
        if normalized_total_timeout is None:
            raise HookRegistrationError(
                "bridge_total_timeout_seconds must be a positive number"
            )
        self._state_lock = RLock()
        self._registry: AsyncHookRegistry | None = registry
        self._event_loop: asyncio.AbstractEventLoop | None = event_loop
        self._closed = False
        self._bridge_total_timeout_seconds = normalized_total_timeout

    def register(
        self,
        event_name: HookName,
        callback,
        *,
        hook_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> HookRegistration:
        """桥接链路不可变，不在 Cron 侧附加本地 Hook。"""
        raise HookRegistrationError("SyncObservationBridge is immutable")

    def emit(self, event: HookEvent) -> HookDispatchResult:
        """只转发三类 Observation，其他事件保持空分发。"""
        if not isinstance(event, HookEvent):
            raise TypeError("event must be a HookEvent")
        if event.name not in _OBSERVATION_EVENTS:
            return HookDispatchResult(event=event, results=())
        with self._state_lock:
            registry = self._registry
            event_loop = self._event_loop
            closed = self._closed
        return _forward_observation_event(
            event,
            registry=registry,
            event_loop=event_loop,
            closed=closed,
            timeout_seconds=self._bridge_total_timeout_seconds,
        )

    def close(self) -> None:
        """清除公共 Registry 与事件循环引用，不遗留桥接资源。"""
        with self._state_lock:
            self._closed = True
            self._registry = None
            self._event_loop = None


class SyncControlBridge(SyncHookRegistry):
    """把同步子 Agent 的控制与观察事件提交回原异步事件循环。"""

    def __init__(
        self,
        registry: AsyncHookRegistry,
        event_loop: asyncio.AbstractEventLoop,
        *,
        bridge_total_timeout_seconds: float = (
            DEFAULT_BRIDGE_TOTAL_TIMEOUT_SECONDS
        ),
    ) -> None:
        """在 Gateway 事件循环线程中固定控制 Hook 快照。"""
        super().__init__()
        if not isinstance(registry, AsyncHookRegistry):
            raise TypeError("registry must be an AsyncHookRegistry")
        if event_loop.is_closed() or not event_loop.is_running():
            raise RuntimeError("event_loop must be running")
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError(
                "SyncControlBridge must be created on event_loop"
            ) from exc
        if current_loop is not event_loop:
            raise RuntimeError("SyncControlBridge must be created on event_loop")
        normalized_total_timeout = _normalize_timeout(
            bridge_total_timeout_seconds
        )
        assert normalized_total_timeout is not None

        snapshots = registry._control_snapshot(
            _CONTROL_EVENTS,
            fallback_timeout_seconds=_BRIDGE_DEFAULT_TIMEOUT_SECONDS,
        )
        by_event: dict[HookName, tuple[_AsyncControlSnapshot, ...]] = {}
        for event_name in _CONTROL_EVENTS:
            by_event[event_name] = tuple(
                snapshot
                for snapshot in snapshots
                if snapshot.event_name == event_name
            )
        self._state_lock = RLock()
        self._registry: AsyncHookRegistry | None = registry
        self._event_loop: asyncio.AbstractEventLoop | None = event_loop
        self._snapshots = by_event
        self._closed = False
        self._retained = False
        self._bridge_total_timeout_seconds = normalized_total_timeout
        self._total_wait_seconds = {
            event_name: self._calculate_total_wait(by_event[event_name])
            for event_name in _CONTROL_EVENTS
        }

    def _calculate_total_wait(
        self,
        snapshots: tuple[_AsyncControlSnapshot, ...],
    ) -> float:
        """根据各 Hook 的有效超时计算同步线程可等待的总上限。"""
        total = sum(
            snapshot.timeout_seconds or _BRIDGE_DEFAULT_TIMEOUT_SECONDS
            for snapshot in snapshots
        )
        return min(
            total + _BRIDGE_OVERHEAD_SECONDS,
            self._bridge_total_timeout_seconds,
        )

    def register(
        self,
        event_name: HookName,
        callback,
        *,
        hook_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> HookRegistration:
        """桥接快照不可变，禁止在子 Agent 侧新增或替换控制规则。"""
        raise HookRegistrationError("SyncControlBridge is immutable")

    def emit_control(self, event: HookEvent) -> HookControlDispatchResult:
        """从同步 Delegate 线程将控制分发提交回原始事件循环。"""
        if not isinstance(event, HookEvent):
            raise TypeError("event must be a HookEvent")
        if event.name not in _CONTROL_EVENTS:
            return self._failure_result(event)
        try:
            if asyncio.get_running_loop() is self._event_loop:
                # 在原事件循环线程中同步等待会死锁，按失败默认阻止处理。
                return self._failure_result(event)
        except RuntimeError:
            pass

        with self._state_lock:
            registry = self._registry
            event_loop = self._event_loop
            snapshots = self._snapshots.get(event.name, ())
            closed = self._closed
        if (
            closed
            or registry is None
            or event_loop is None
            or event_loop.is_closed()
            or not event_loop.is_running()
        ):
            return self._failure_result(event)
        if not snapshots:
            return build_control_dispatch_result(event, [])

        coroutine = self._dispatch_snapshot(registry, event, snapshots)
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, event_loop)
        except Exception:
            coroutine.close()
            logger.exception("Control Hook bridge submission failed")
            return self._failure_result(event)
        try:
            return future.result(
                timeout=self._total_wait_seconds[event.name]
            )
        except concurrent.futures.TimeoutError:
            future.cancel()
            self._consume_future(future)
            logger.warning("Control Hook bridge timed out")
            return self._failure_result(event)
        except Exception:
            self._consume_future(future)
            logger.exception("Control Hook bridge dispatch failed")
            return self._failure_result(event)

    def emit(self, event: HookEvent) -> HookDispatchResult:
        """把同步 Delegate 的只读观察事件转发给原 Async Registry。"""
        if not isinstance(event, HookEvent):
            raise TypeError("event must be a HookEvent")
        if event.name not in _OBSERVATION_EVENTS:
            return HookDispatchResult(event=event, results=())
        with self._state_lock:
            registry = self._registry
            event_loop = self._event_loop
            closed = self._closed
        return _forward_observation_event(
            event,
            registry=registry,
            event_loop=event_loop,
            closed=closed,
            timeout_seconds=self._bridge_total_timeout_seconds,
        )

    async def _dispatch_snapshot(
        self,
        registry: AsyncHookRegistry,
        event: HookEvent,
        snapshots: tuple[_AsyncControlSnapshot, ...],
    ) -> HookControlDispatchResult:
        """仅在桥接创建时所属事件循环内复用 Async Registry 控制语义。"""
        try:
            return await registry._emit_control_snapshot(event, snapshots)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Control Hook bridge snapshot failed")
            return self._failure_result(event)

    @staticmethod
    def _consume_future(
        future: concurrent.futures.Future[HookControlDispatchResult],
    ) -> None:
        """安全消费延迟 Future 的最终结果，避免未观察异常。"""
        def consume(completed: concurrent.futures.Future[HookControlDispatchResult]) -> None:
            try:
                completed.result()
            except concurrent.futures.CancelledError:
                return
            except Exception:
                logger.exception("Detached control Hook bridge future failed")

        future.add_done_callback(consume)

    @staticmethod
    def _consume_observation_future(
        future: concurrent.futures.Future[HookDispatchResult],
    ) -> None:
        """消费已脱离等待的观察分发结果，不记录异常正文。"""
        _consume_observation_future(future)

    @staticmethod
    def _failure_result(event: HookEvent) -> HookControlDispatchResult:
        """构造不泄露桥接和 Plugin 内部细节的失败默认阻止结果。"""
        return build_control_dispatch_result(
            event,
            [
                HookInvocationResult(
                    hook_id="control_bridge",
                    success=False,
                    error_type="HookControlBridgeError",
                    error_message="hook control bridge failed",
                )
            ],
            block_reason=control_failure_reason(),
        )

    @staticmethod
    def _observation_failure_result(event: HookEvent) -> HookDispatchResult:
        """构造不会改变子 Agent 业务结果的观察桥接失败摘要。"""
        return _observation_failure_result(event)

    def close(self) -> None:
        """释放本次子运行持有的 Registry、快照和事件循环引用。"""
        with self._state_lock:
            self._closed = True
            self._retained = False
            self._snapshots = {}
            self._registry = None
            self._event_loop = None

    def retain_for_background_delegate(self) -> None:
        """将桥接器所有权转交给后台 Delegate worker 的 finally 清理。"""
        with self._state_lock:
            if self._closed:
                raise RuntimeError("SyncControlBridge is closed")
            self._retained = True

    @property
    def retained_for_background_delegate(self) -> bool:
        """说明桥接器已转交后台任务，当前工具分发不得提前关闭它。"""
        with self._state_lock:
            return self._retained


def build_sync_control_bridge(
    registry: AsyncHookRegistry,
    *,
    bridge_total_timeout_seconds: float = DEFAULT_BRIDGE_TOTAL_TIMEOUT_SECONDS,
) -> SyncControlBridge:
    """在当前 Gateway 事件循环中创建一次同步 Delegate 控制快照。"""
    return SyncControlBridge(
        registry,
        asyncio.get_running_loop(),
        bridge_total_timeout_seconds=bridge_total_timeout_seconds,
    )


def build_sync_observation_bridge(
    registry: AsyncHookRegistry,
    *,
    bridge_total_timeout_seconds: float = DEFAULT_BRIDGE_TOTAL_TIMEOUT_SECONDS,
) -> SyncObservationBridge:
    """在当前 Gateway 事件循环中创建生命周期级观察桥。"""
    return SyncObservationBridge(
        registry,
        asyncio.get_running_loop(),
        bridge_total_timeout_seconds=bridge_total_timeout_seconds,
    )
