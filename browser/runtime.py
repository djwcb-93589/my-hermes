"""BrowserSession 的固定线程运行时。

同步 Playwright 对象只能在创建它的线程中使用。本模块把每个浏览器会话固定
在一个工作线程内，通过队列串行执行请求；它不依赖 Hermes 的工具或审批层。
"""

from __future__ import annotations

import atexit
import json
import queue
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Callable

from browser.session import BrowserSession


def _error(error_type: str, error: str) -> str:
    """把运行时异常收敛为不会中断调用方的稳定 JSON。"""
    return json.dumps(
        {"ok": False, "error_type": error_type, "error": error},
        ensure_ascii=False,
    )


@dataclass(slots=True)
class _WorkItem:
    """一条只在所属 worker 线程执行的方法调用。"""

    method: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    future: Future[str]


class BrowserWorker:
    """独占一个 BrowserSession 的固定工作线程。"""

    def __init__(
        self,
        session_key: str,
        *,
        workspace_root: str | Path | None = None,
        artifact_dir: str | Path | None = None,
        headless: bool = True,
        channel: str | None = "chrome",
        on_failure: Callable[[str, "BrowserWorker"], None] | None = None,
    ) -> None:
        self.session_key = session_key
        self._workspace_root = workspace_root
        self._artifact_dir = artifact_dir
        self._headless = headless
        self._channel = channel
        self._on_failure = on_failure
        self._queue: queue.Queue[_WorkItem | None] = queue.Queue()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._failed = False
        self._failure_message = ""
        self._last_used = monotonic()
        self._thread = threading.Thread(
            target=self._run,
            name=f"browser-worker-{session_key[:24]}",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()

    @property
    def last_used(self) -> float:
        """返回最近一次提交或执行完成的单调时间。"""
        return self._last_used

    @property
    def failed(self) -> bool:
        """worker 启动或工作线程已永久失效时为真。"""
        return self._failed

    def call(
        self,
        method: str,
        *args: Any,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> str:
        """串行执行 BrowserSession 公开方法，不把 Playwright 交给线程池。"""
        if not isinstance(method, str) or not method or method.startswith("_"):
            return _error("invalid_args", "browser method is invalid")
        if self._closed.is_set() or self._failed:
            return _error(
                "browser_worker_unavailable",
                self._failure_message or "browser worker is closed",
            )
        future: Future[str] = Future()
        self._last_used = monotonic()
        self._queue.put(_WorkItem(method, args, dict(kwargs), future))
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            return _error("browser_worker_timeout", "browser worker did not finish in time")
        except Exception as exc:
            return _error("browser_worker_failed", f"browser worker failed: {exc.__class__.__name__}")

    def close(self) -> None:
        """请求线程完成已有任务后关闭浏览器；重复调用安全。"""
        if self._closed.is_set():
            return
        self._closed.set()
        self._queue.put(None)
        if threading.get_ident() != self._thread.ident:
            self._thread.join(timeout=10)

    def _run(self) -> None:
        """只在固定线程创建、调用和释放同步 Playwright 对象。"""
        session: BrowserSession | None = None
        try:
            session = BrowserSession(
                headless=self._headless,
                channel=self._channel,
                workspace_root=self._workspace_root,
                artifact_dir=self._artifact_dir,
            )
            session.start()
        except Exception as exc:
            self._failed = True
            self._failure_message = f"browser startup failed: {exc.__class__.__name__}"
            self._ready.set()
            return
        self._ready.set()
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                if item.future.cancelled():
                    continue
                try:
                    target = getattr(session, item.method, None)
                    if target is None or not callable(target):
                        result = _error("unsupported_browser_operation", "browser operation is unavailable")
                    else:
                        result = target(*item.args, **item.kwargs)
                        if not isinstance(result, str):
                            result = _error("browser_worker_failed", "browser operation returned an invalid result")
                except Exception as exc:
                    result = _error(
                        "browser_operation_failed",
                        f"browser operation failed: {exc.__class__.__name__}",
                    )
                if not item.future.done():
                    item.future.set_result(result)
                self._last_used = monotonic()
        except BaseException as exc:
            self._failed = True
            self._failure_message = f"browser worker crashed: {exc.__class__.__name__}"
            self._notify_failure()
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
            self._closed.set()
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is not None and not item.future.done():
                    item.future.set_result(
                        _error("browser_worker_unavailable", "browser worker is closed")
                    )

    def _notify_failure(self) -> None:
        """只通知管理器移除失效 worker，通知异常不能影响清理。"""
        if self._on_failure is not None:
            try:
                self._on_failure(self.session_key, self)
            except Exception:
                pass


class BrowserManager:
    """按可信 session_key 隔离并复用 BrowserWorker。"""

    def __init__(
        self,
        *,
        idle_timeout_seconds: float = 1800.0,
        workspace_root: str | Path | None = None,
        artifact_dir: str | Path | None = None,
        headless: bool = True,
        channel: str | None = "chrome",
    ) -> None:
        self._idle_timeout_seconds = max(0.0, float(idle_timeout_seconds))
        self._workspace_root = workspace_root
        self._artifact_dir = artifact_dir
        self._headless = headless
        self._channel = channel
        self._workers: dict[str, BrowserWorker] = {}
        self._lock = threading.Lock()
        self._stop_reaper = threading.Event()
        self._reaper = threading.Thread(
            target=self._reap_idle_workers,
            name="browser-idle-reaper",
            daemon=True,
        )
        self._reaper.start()

    def get_worker(self, session_key: str) -> BrowserWorker:
        """同一会话并发请求只创建一个 worker，不允许模型提供 session_key。"""
        normalized_key = str(session_key or "").strip()
        if not normalized_key:
            raise ValueError("browser session_key is required")
        with self._lock:
            self._cleanup_idle_locked()
            worker = self._workers.get(normalized_key)
            if worker is not None and not worker.failed:
                return worker
            if worker is not None:
                self._workers.pop(normalized_key, None)
            worker = BrowserWorker(
                normalized_key,
                workspace_root=self._workspace_root,
                artifact_dir=self._artifact_dir,
                headless=self._headless,
                channel=self._channel,
                on_failure=self._remove_failed_worker,
            )
            if worker.failed:
                raise RuntimeError("browser worker startup failed")
            self._workers[normalized_key] = worker
            return worker

    def close_session(self, session_key: str) -> bool:
        """关闭一个会话自己的浏览器，不影响其他 Agent 会话。"""
        normalized_key = str(session_key or "").strip()
        with self._lock:
            worker = self._workers.pop(normalized_key, None)
        if worker is None:
            return False
        worker.close()
        return True

    def close_all(self) -> None:
        """退出时统一释放全部独立浏览器线程。"""
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.close()

    def shutdown(self) -> None:
        """停止空闲清理线程并关闭全部浏览器，供进程退出时调用。"""
        self._stop_reaper.set()
        self.close_all()

    def cleanup_idle(self) -> int:
        """关闭超过空闲时限的会话，返回关闭数量。"""
        with self._lock:
            return self._cleanup_idle_locked()

    def _cleanup_idle_locked(self) -> int:
        if self._idle_timeout_seconds <= 0:
            return 0
        now = monotonic()
        stale = [
            key
            for key, worker in self._workers.items()
            if worker.failed or now - worker.last_used >= self._idle_timeout_seconds
        ]
        workers = [self._workers.pop(key) for key in stale]
        # 不能持有 Manager 锁等待线程退出；close 的 join 在锁外完成。
        if workers:
            threading.Thread(
                target=lambda: [worker.close() for worker in workers],
                name="browser-idle-cleanup",
                daemon=True,
            ).start()
        return len(workers)

    def _remove_failed_worker(self, session_key: str, worker: BrowserWorker) -> None:
        with self._lock:
            if self._workers.get(session_key) is worker:
                self._workers.pop(session_key, None)

    def _reap_idle_workers(self) -> None:
        """没有新请求时也按固定节奏回收长期空闲的会话。"""
        interval = min(60.0, max(1.0, self._idle_timeout_seconds / 4))
        while not self._stop_reaper.wait(interval):
            try:
                self.cleanup_idle()
            except Exception:
                # 清理器不能因单个会话异常退出，下一轮仍继续回收。
                pass


default_browser_manager = BrowserManager()
atexit.register(default_browser_manager.shutdown)
