"""Gateway composition root 使用的轻量关停信号桥。"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable


class GatewayShutdownSignalController:
    """保存并恢复本次 Gateway 运行期间替换的信号处理器。"""

    __slots__ = ("_closed", "_loop", "_registrations")

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        registrations: list[tuple[int, object, bool]],
    ) -> None:
        self._loop = loop
        self._registrations = registrations
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for signal_number, previous_handler, installed_on_loop in reversed(
            self._registrations
        ):
            if installed_on_loop:
                try:
                    self._loop.remove_signal_handler(signal_number)
                except (NotImplementedError, RuntimeError, ValueError):
                    pass
            try:
                signal.signal(signal_number, previous_handler)
            except (OSError, RuntimeError, TypeError, ValueError):
                pass
        self._registrations.clear()


def install_gateway_shutdown_signals(
    shutdown_event: asyncio.Event,
) -> GatewayShutdownSignalController:
    """把平台关停信号转换为 event，不在 handler 中执行资源清理。"""
    if not isinstance(shutdown_event, asyncio.Event):
        raise TypeError("gateway shutdown event is invalid")
    loop = asyncio.get_running_loop()
    registrations: list[tuple[int, object, bool]] = []

    def request_shutdown(_signum: int, _frame: object) -> None:
        loop.call_soon_threadsafe(shutdown_event.set)

    seen: set[int] = set()
    for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signal_value = getattr(signal, signal_name, None)
        if signal_value is None:
            continue
        signal_number = int(signal_value)
        if signal_number in seen:
            continue
        seen.add(signal_number)
        try:
            previous_handler = signal.getsignal(signal_number)
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        try:
            loop.add_signal_handler(signal_number, shutdown_event.set)
        except (NotImplementedError, OSError, RuntimeError, ValueError):
            try:
                signal.signal(signal_number, request_shutdown)
            except (OSError, RuntimeError, TypeError, ValueError):
                continue
            registrations.append((signal_number, previous_handler, False))
        else:
            registrations.append((signal_number, previous_handler, True))
    return GatewayShutdownSignalController(loop, registrations)


async def wait_for_gateway_shutdown(
    shutdown_event: asyncio.Event,
    timeout_seconds: float,
) -> bool:
    """在保留原轮询节奏的同时优先响应关停事件。"""
    if shutdown_event.is_set():
        return True
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=timeout_seconds)
    except TimeoutError:
        return False
    return True


async def start_gateway_until_shutdown(
    start_gateway: Callable[[], Awaitable[object]],
    shutdown_event: asyncio.Event,
) -> bool:
    """等待启动完成或关停事件；关停获胜时取消启动并交给外层 stop。"""
    if not callable(start_gateway):
        raise TypeError("gateway start operation is invalid")
    if shutdown_event.is_set():
        return False
    start_task = asyncio.create_task(start_gateway())
    shutdown_task = asyncio.create_task(shutdown_event.wait())
    try:
        done, _pending = await asyncio.wait(
            (start_task, shutdown_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if start_task in done:
            await start_task
            return not shutdown_event.is_set()
        await _cancel_task(start_task)
        return False
    except BaseException:
        await _cancel_task(start_task)
        raise
    finally:
        await _cancel_task(shutdown_task)


async def _cancel_task(task: asyncio.Task) -> None:
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


__all__ = [
    "GatewayShutdownSignalController",
    "install_gateway_shutdown_signals",
    "start_gateway_until_shutdown",
    "wait_for_gateway_shutdown",
]
