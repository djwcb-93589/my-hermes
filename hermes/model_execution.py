"""平台无关的同步模型调用与流消费边界。"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable


logger = logging.getLogger(__name__)


class ModelExecutionCancelled(Exception):
    """当前调用方已请求停止本地等待。"""


class ModelExecutionTimedOut(Exception):
    """等待同步模型调用超过调用方提供的超时。"""


@dataclass(frozen=True)
class _CallOutcome:
    """后台同步调用的私有结果。"""

    succeeded: bool
    value: object


@dataclass(frozen=True)
class _StreamOutcome:
    """后台同步流消费者的私有终态。"""

    succeeded: bool
    value: object | None = None


class _StreamProgress:
    """在线程之间安全维护最近一次取得模型 chunk 的时间。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_progress_at = time.monotonic()

    def mark_started(self) -> None:
        """从后台线程开始创建 stream 的时刻重新开始等待窗口。"""
        now = time.monotonic()
        with self._lock:
            self._last_progress_at = now

    def mark_chunk_received(self) -> None:
        """记录同步 stream 成功返回一个 chunk 的时刻。"""
        now = time.monotonic()
        with self._lock:
            self._last_progress_at = now

    def remaining(self, timeout_seconds: float | None) -> float | None:
        """返回距离最近一次进展还剩多少等待时间。"""
        if timeout_seconds is None:
            return None
        now = time.monotonic()
        with self._lock:
            last_progress_at = self._last_progress_at
        return timeout_seconds - (now - last_progress_at)


class _StreamState:
    """在线程之间安全交接 stream，并把关闭请求限制为一次。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stream = None
        self._close_requested = False
        self._close_started = False

    def set_stream(self, stream) -> None:
        with self._lock:
            self._stream = stream
            close_requested = self._close_requested
        if close_requested:
            self.request_close()

    def request_close(self) -> None:
        with self._lock:
            self._close_requested = True
            stream = self._stream
            if stream is None or self._close_started:
                return
            self._close_started = True

        # close() 属于同步 SDK，不能让调用线程等待一个不可控的关闭动作。
        closer = threading.Thread(
            target=_close_sync_stream,
            args=(stream,),
            name="hermes-model-stream-close",
            daemon=True,
        )
        try:
            closer.start()
        except Exception:
            # 极端情况下线程创建失败时放弃关闭，不覆盖调用方原始异常。
            logger.warning("Synchronous model stream close thread failed")


def _close_sync_stream(stream) -> None:
    """尽力调用同步 stream 的 close()，不暴露关闭异常细节。"""
    if stream is None:
        return
    close = getattr(stream, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception as exc:
        # 日志只记录异常类型，不记录 chunk、请求参数或远端响应。
        logger.warning(
            "Synchronous model stream close failed: %s",
            type(exc).__name__,
        )


def _normalize_poll_interval(poll_interval: float) -> float:
    try:
        value = float(poll_interval)
    except (TypeError, ValueError):
        raise ValueError("poll_interval must be greater than 0") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError("poll_interval must be greater than 0")
    return value


def _normalize_timeout(timeout_seconds: float | None) -> float | None:
    if timeout_seconds is None:
        return None
    try:
        value = float(timeout_seconds)
    except (TypeError, ValueError):
        raise ValueError("timeout_seconds must be greater than 0") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout_seconds must be greater than 0")
    return value


def _is_cancelled(cancel_checker: Callable[[], bool] | None) -> bool:
    return cancel_checker is not None and bool(cancel_checker())


def _raise_cancelled(stop_event: threading.Event) -> None:
    stop_event.set()
    raise ModelExecutionCancelled


def _raise_timed_out(stop_event: threading.Event) -> None:
    stop_event.set()
    raise ModelExecutionTimedOut


def run_interruptible_call(
    request_callable: Callable[[], object],
    *,
    cancel_checker: Callable[[], bool] | None,
    timeout_seconds: float | None,
    poll_interval: float = 0.05,
):
    """在 daemon 线程中执行同步调用，并可中断本地等待。"""
    if not callable(request_callable):
        raise TypeError("request_callable must be callable")
    poll_interval = _normalize_poll_interval(poll_interval)
    timeout_seconds = _normalize_timeout(timeout_seconds)

    outcome_queue: queue.Queue[_CallOutcome] = queue.Queue(maxsize=1)
    stop_event = threading.Event()
    if _is_cancelled(cancel_checker):
        _raise_cancelled(stop_event)

    def worker() -> None:
        try:
            value = request_callable()
        except BaseException as exc:
            outcome = _CallOutcome(False, exc)
        else:
            outcome = _CallOutcome(True, value)

        # 调用方已经放弃等待时，后台结果不再进入任何上层对象。
        if stop_event.is_set():
            return
        try:
            outcome_queue.put_nowait(outcome)
        except queue.Full:
            pass

    thread = threading.Thread(
        target=worker,
        name="hermes-model-call",
        daemon=True,
    )
    thread.start()
    deadline = (
        None
        if timeout_seconds is None
        else time.monotonic() + timeout_seconds
    )

    while True:
        if _is_cancelled(cancel_checker):
            _raise_cancelled(stop_event)

        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # 取消与超时同时到达时，优先采用已经观察到的取消。
                if _is_cancelled(cancel_checker):
                    _raise_cancelled(stop_event)
                _raise_timed_out(stop_event)
            wait_seconds = min(poll_interval, remaining)
        else:
            wait_seconds = poll_interval

        try:
            outcome = outcome_queue.get(timeout=wait_seconds)
        except queue.Empty:
            continue

        # 结果到达与取消同时发生时，调用方仍以已观察到的取消为准。
        if _is_cancelled(cancel_checker):
            _raise_cancelled(stop_event)
        if outcome.succeeded:
            return outcome.value
        raise outcome.value


def consume_interruptible_stream(
    stream_factory: Callable[[], object],
    *,
    cancel_checker: Callable[[], bool] | None,
    timeout_seconds: float | None,
    on_chunk: Callable[[object], object],
    poll_interval: float = 0.05,
    queue_size: int = 16,
) -> None:
    """在单个 daemon 消费线程中消费同步流，并向调用方交付原始 chunk。"""
    if not callable(stream_factory):
        raise TypeError("stream_factory must be callable")
    if not callable(on_chunk):
        raise TypeError("on_chunk must be callable")
    poll_interval = _normalize_poll_interval(poll_interval)
    timeout_seconds = _normalize_timeout(timeout_seconds)
    if isinstance(queue_size, bool) or not isinstance(queue_size, int):
        raise ValueError("queue_size must be greater than 0")
    if queue_size <= 0:
        raise ValueError("queue_size must be greater than 0")

    chunk_queue: queue.Queue[object] = queue.Queue(maxsize=queue_size)
    outcome_queue: queue.Queue[_StreamOutcome] = queue.Queue(maxsize=1)
    stop_event = threading.Event()
    stream_state = _StreamState()
    stream_progress = _StreamProgress()
    if _is_cancelled(cancel_checker):
        _raise_cancelled(stop_event)

    def report(outcome: _StreamOutcome) -> None:
        try:
            outcome_queue.put_nowait(outcome)
        except queue.Full:
            pass

    def worker() -> None:
        try:
            stream_progress.mark_started()
            stream = stream_factory()
            stream_state.set_stream(stream)
            if stop_event.is_set():
                return
            for chunk in stream:
                # 必须在 Queue put 前记录，避免本地背压被误判为模型无进展。
                stream_progress.mark_chunk_received()
                if stop_event.is_set():
                    return
                while not stop_event.is_set():
                    try:
                        chunk_queue.put(chunk, timeout=poll_interval)
                        break
                    except queue.Full:
                        continue
                if stop_event.is_set():
                    return
        except BaseException as exc:
            if not stop_event.is_set():
                report(_StreamOutcome(False, exc))
        else:
            if not stop_event.is_set():
                report(_StreamOutcome(True))
        finally:
            stream_state.request_close()

    thread = threading.Thread(
        target=worker,
        name="hermes-model-stream",
        daemon=True,
    )
    thread.start()

    def abort_if_needed() -> None:
        if _is_cancelled(cancel_checker):
            stop_event.set()
            stream_state.request_close()
            raise ModelExecutionCancelled
        remaining = stream_progress.remaining(timeout_seconds)
        if remaining is not None and remaining <= 0:
            # 超时边界再次检查取消，保证取消优先级高于超时。
            if _is_cancelled(cancel_checker):
                stop_event.set()
                stream_state.request_close()
                raise ModelExecutionCancelled
            stop_event.set()
            stream_state.request_close()
            raise ModelExecutionTimedOut

    def remaining_wait() -> float:
        remaining = stream_progress.remaining(timeout_seconds)
        if remaining is None:
            return poll_interval
        if remaining <= 0:
            abort_if_needed()
        return min(poll_interval, remaining)

    def deliver(chunk) -> None:
        abort_if_needed()
        try:
            on_chunk(chunk)
        except BaseException:
            stop_event.set()
            stream_state.request_close()
            raise

    while True:
        abort_if_needed()
        try:
            chunk = chunk_queue.get_nowait()
        except queue.Empty:
            try:
                outcome = outcome_queue.get_nowait()
            except queue.Empty:
                try:
                    chunk = chunk_queue.get(timeout=remaining_wait())
                except queue.Empty:
                    continue
                deliver(chunk)
                continue

            # 消费线程先写入全部 chunk，后写入终态；终态到达后仍需排空队列。
            while True:
                abort_if_needed()
                try:
                    pending_chunk = chunk_queue.get_nowait()
                except queue.Empty:
                    break
                deliver(pending_chunk)
            if outcome.succeeded:
                abort_if_needed()
                return None
            raise outcome.value
        else:
            deliver(chunk)
