"""BrowserSession 的固定线程运行时。

同步 Playwright 对象只能在创建它的线程中使用。本模块把每个浏览器会话固定
在一个工作线程内，通过队列串行执行请求；它不依赖 Hermes 的工具或审批层。
"""

from __future__ import annotations

import atexit
import hashlib
import json
import math
import queue
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Callable

from browser.session import BrowserSession


_SUPPORTED_BROWSER_CHANNELS = frozenset({
    "chrome",
    "chrome-beta",
    "chrome-dev",
    "chrome-canary",
    "msedge",
    "msedge-beta",
    "msedge-dev",
    "msedge-canary",
})


def _default_artifact_root() -> Path:
    """返回 browser 包内预先约定的下载与截图目录根。"""
    return Path(__file__).resolve().parent


class BrowserRuntimeError(RuntimeError):
    """运行时配置或 worker 生命周期错误的稳定分类。"""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message


def _positive_seconds(value: object, name: str) -> float:
    """校验运行时等待时长，拒绝布尔值、非有限值和非正数。"""
    if isinstance(value, bool):
        raise BrowserRuntimeError("invalid_browser_config", f"{name} is invalid")
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise BrowserRuntimeError("invalid_browser_config", f"{name} is invalid") from exc
    if not math.isfinite(normalized) or normalized <= 0:
        raise BrowserRuntimeError("invalid_browser_config", f"{name} is invalid")
    return normalized


def _normalize_channel(value: object) -> str | None:
    """只接受 Playwright 已知 channel；空值明确表示内置 Chromium。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise BrowserRuntimeError("invalid_browser_config", "browser channel is invalid")
    channel = value.strip() or None
    if channel is not None and channel not in _SUPPORTED_BROWSER_CHANNELS:
        raise BrowserRuntimeError("invalid_browser_config", "browser channel is invalid")
    return channel


def _error(error_type: str, error: str, **extra: Any) -> str:
    """把运行时异常收敛为不会中断调用方的稳定 JSON。"""
    return json.dumps(
        {"ok": False, "error_type": error_type, "error": error, **extra},
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
        artifact_root: str | Path | None = None,
        artifact_dir: str | Path | None = None,
        headless: bool = True,
        channel: str | None = None,
        startup_timeout_seconds: float = 30.0,
        operation_timeout_seconds: float = 60.0,
        on_failure: Callable[[str, "BrowserWorker"], None] | None = None,
    ) -> None:
        self.session_key = session_key
        self._workspace_root = (
            Path(workspace_root).resolve()
            if workspace_root is not None
            else Path.cwd().resolve()
        )
        self._artifact_root = (
            Path(artifact_root).expanduser().resolve()
            if artifact_root is not None
            else _default_artifact_root().resolve()
        )
        self._artifact_dir_config = self._artifact_dir_config_for_session(
            session_key,
            artifact_dir,
        )
        # BrowserSession 的 artifact_dir 只接受相对配置；绝对根目录只由运行时提供。
        self._artifact_dir = (self._artifact_root / self._artifact_dir_config).resolve()
        if not isinstance(headless, bool):
            raise BrowserRuntimeError("invalid_browser_config", "browser headless is invalid")
        self._headless = headless
        self._channel = _normalize_channel(channel)
        self._startup_timeout_seconds = _positive_seconds(
            startup_timeout_seconds,
            "browser startup timeout",
        )
        self._operation_timeout_seconds = _positive_seconds(
            operation_timeout_seconds,
            "browser operation timeout",
        )
        self._on_failure = on_failure
        self._queue: queue.Queue[_WorkItem | None] = queue.Queue()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._state_lock = threading.Lock()
        self._busy = False
        self._queued = 0
        self._failed = False
        self._failure_error_type = "browser_worker_unavailable"
        self._failure_message = ""
        self._last_used = monotonic()
        self._thread = threading.Thread(
            target=self._run,
            name=f"browser-worker-{self._session_digest(session_key)}",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=self._startup_timeout_seconds):
            self._invalidate(
                "browser_worker_startup_timeout",
                "browser worker startup timed out",
            )
            self._close_async()

    @property
    def last_used(self) -> float:
        """返回最近一次提交或执行完成的单调时间。"""
        return self._last_used

    @property
    def failed(self) -> bool:
        """worker 启动或工作线程已永久失效时为真。"""
        return self._failed

    @property
    def failure_error_type(self) -> str:
        """返回失效原因的稳定错误类型，供 Manager 传给可信调用方。"""
        return self._failure_error_type

    @property
    def failure_message(self) -> str:
        """返回不含线程、路径和 Playwright 细节的失败说明。"""
        return self._failure_message

    @property
    def workspace_root(self) -> Path:
        """返回创建后固定不变的工作区配置，仅供 Manager 校验复用。"""
        return Path(self._workspace_root).resolve()

    @property
    def artifact_dir(self) -> Path:
        """返回内部解析后的会话产物目录，仅供运行时校验与清理。"""
        return Path(self._artifact_dir).resolve()

    @property
    def artifact_root(self) -> Path:
        """返回固定的浏览器产物根目录，仅供运行时内部使用。"""
        return Path(self._artifact_root).resolve()

    @property
    def artifact_dir_config(self) -> Path:
        """返回交给 BrowserSession 的相对产物目录配置。"""
        return Path(self._artifact_dir_config)

    @staticmethod
    def _session_digest(session_key: str) -> str:
        """生成不泄露原始会话标识的稳定短摘要。"""
        return hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:20]

    @classmethod
    def _artifact_dir_config_for_session(
        cls,
        session_key: str,
        artifact_dir: str | Path | None,
    ) -> Path:
        """只接受类型目录共同根，文件名本身以 UUID 保持跨会话唯一。"""
        expected = Path(".")
        if artifact_dir is None:
            return expected
        configured = Path(artifact_dir)
        if (
            configured.is_absolute()
            or ".." in configured.parts
            or configured != expected
        ):
            raise ValueError("browser artifact_dir must be the shared relative artifact root")
        return configured

    def is_idle(self, *, now: float, idle_timeout_seconds: float) -> bool:
        """仅在没有执行或排队请求且超过时限时允许 Manager 回收。"""
        with self._state_lock:
            return (
                not self._busy
                and self._queued == 0
                and now - self._last_used >= idle_timeout_seconds
            )

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
        future: Future[str] = Future()
        effective_timeout = self._operation_timeout_seconds if timeout is None else timeout
        try:
            effective_timeout = _positive_seconds(
                effective_timeout,
                "browser operation timeout",
            )
        except BrowserRuntimeError:
            return _error("invalid_args", "browser operation timeout is invalid")
        with self._state_lock:
            if self._closed.is_set() or self._failed:
                return _error(
                    "browser_worker_unavailable",
                    "browser worker is unavailable",
                )
            self._queued += 1
            self._last_used = monotonic()
            self._queue.put(_WorkItem(method, args, dict(kwargs), future))
        try:
            return future.result(timeout=effective_timeout)
        except FutureTimeoutError:
            self._invalidate(
                "browser_worker_timeout",
                "browser operation timed out",
            )
            self._close_async()
            return _error(
                "browser_worker_timeout",
                "browser operation timed out; its outcome is unknown",
                execution_state="unknown",
            )
        except Exception:
            return _error("browser_worker_failed", "browser worker failed")

    def close(self) -> None:
        """请求线程完成已有任务后关闭浏览器；重复调用安全。"""
        with self._state_lock:
            if not self._closed.is_set():
                self._closed.set()
                self._queue.put(None)
        if threading.get_ident() != self._thread.ident:
            self._thread.join(timeout=10)

    def close_if_idle(self, *, now: float, idle_timeout_seconds: float) -> bool:
        """原子地确认空闲并封闭 worker，防止回收与新请求竞争。"""
        with self._state_lock:
            if (
                self._closed.is_set()
                or self._busy
                or self._queued != 0
                or now - self._last_used < idle_timeout_seconds
            ):
                return False
            self._closed.set()
            self._queue.put(None)
            return True

    def _run(self) -> None:
        """只在固定线程创建、调用和释放同步 Playwright 对象。"""
        session: BrowserSession | None = None
        try:
            session = BrowserSession(
                headless=self._headless,
                channel=self._channel,
                workspace_root=self._workspace_root,
                artifact_root=self._artifact_root,
                artifact_dir=self._artifact_dir_config,
            )
            session.start()
        except Exception:
            self._mark_failed(
                "browser_worker_startup_failed",
                "browser worker startup failed",
            )
            self._notify_failure_async()
            self._ready.set()
            return
        self._ready.set()
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                with self._state_lock:
                    self._queued = max(0, self._queued - 1)
                    self._busy = True
                if item.future.cancelled():
                    with self._state_lock:
                        self._busy = False
                        self._last_used = monotonic()
                    continue
                try:
                    target = getattr(session, item.method, None)
                    if target is None or not callable(target):
                        result = _error("unsupported_browser_operation", "browser operation is unavailable")
                    else:
                        result = target(*item.args, **item.kwargs)
                        if not isinstance(result, str):
                            result = _error("browser_worker_failed", "browser operation returned an invalid result")
                except Exception:
                    result = _error("browser_operation_failed", "browser operation failed")
                if not item.future.done():
                    item.future.set_result(result)
                with self._state_lock:
                    self._busy = False
                    self._last_used = monotonic()
        except BaseException:
            self._mark_failed("browser_worker_failed", "browser worker failed")
            self._notify_failure_async()
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

    def _mark_failed(self, error_type: str, message: str) -> bool:
        """只允许首次失效原因决定 worker 的后续可见状态。"""
        with self._state_lock:
            if self._failed:
                return False
            self._failed = True
            self._failure_error_type = error_type
            self._failure_message = message
        return True

    def _invalidate(self, error_type: str, message: str) -> None:
        """封闭失效 worker，取消尚未开始的请求并让 Manager 立即移除它。"""
        if not self._mark_failed(error_type, message):
            return
        pending: list[_WorkItem] = []
        with self._state_lock:
            self._closed.set()
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is not None:
                    pending.append(item)
            self._queued = 0
            self._queue.put(None)
        self._notify_failure_async()
        for item in pending:
            if not item.future.done():
                item.future.set_result(_error(
                    "browser_worker_unavailable",
                    "browser worker is unavailable",
                ))

    def _close_async(self) -> None:
        """超时调用方不能等待可能卡住的浏览器线程退出。"""
        threading.Thread(
            target=self.close,
            name="browser-worker-close",
            daemon=True,
        ).start()

    def _notify_failure_async(self) -> None:
        """失败通知不能反向等待正持有 Manager 锁的启动调用。"""
        threading.Thread(
            target=self._notify_failure,
            name="browser-worker-failure-notify",
            daemon=True,
        ).start()


class BrowserManager:
    """按可信 session_key 隔离并复用 BrowserWorker。"""

    def __init__(
        self,
        *,
        idle_timeout_seconds: float = 1800.0,
        workspace_root: str | Path | None = None,
        artifact_root: str | Path | None = None,
        artifact_dir: str | Path | None = None,
        headless: bool = True,
        channel: str | None = None,
        startup_timeout_seconds: float = 30.0,
        operation_timeout_seconds: float = 60.0,
        _allow_reconfigure_once: bool = False,
    ) -> None:
        if not isinstance(headless, bool):
            raise BrowserRuntimeError("invalid_browser_config", "browser headless is invalid")
        self._idle_timeout_seconds = _positive_seconds(
            idle_timeout_seconds,
            "browser idle timeout",
        )
        self._workspace_root = (
            Path(workspace_root).resolve() if workspace_root is not None else None
        )
        self._artifact_root = (
            Path(artifact_root).expanduser().resolve()
            if artifact_root is not None
            else _default_artifact_root().resolve()
        )
        if artifact_dir is not None:
            raise ValueError("BrowserManager does not accept a shared artifact_dir")
        self._headless = headless
        self._channel = _normalize_channel(channel)
        self._startup_timeout_seconds = _positive_seconds(
            startup_timeout_seconds,
            "browser startup timeout",
        )
        self._operation_timeout_seconds = _positive_seconds(
            operation_timeout_seconds,
            "browser operation timeout",
        )
        self._allow_reconfigure_once = _allow_reconfigure_once
        self._configured = not _allow_reconfigure_once
        self._workers: dict[str, BrowserWorker] = {}
        self._lock = threading.Lock()
        self._stop_reaper = threading.Event()
        self._reaper = threading.Thread(
            target=self._reap_idle_workers,
            name="browser-idle-reaper",
            daemon=True,
        )
        self._reaper.start()

    def configure_once(
        self,
        *,
        idle_timeout_seconds: float,
        headless: bool,
        channel: str | None,
        startup_timeout_seconds: float,
        operation_timeout_seconds: float,
    ) -> None:
        """仅允许默认 Manager 在创建 worker 前接收一次可信运行时配置。"""
        if not self._allow_reconfigure_once:
            raise BrowserRuntimeError(
                "browser_runtime_config_conflict",
                "browser runtime configuration is fixed",
            )
        if not isinstance(headless, bool):
            raise BrowserRuntimeError("invalid_browser_config", "browser headless is invalid")
        normalized = (
            _positive_seconds(idle_timeout_seconds, "browser idle timeout"),
            headless,
            _normalize_channel(channel),
            _positive_seconds(startup_timeout_seconds, "browser startup timeout"),
            _positive_seconds(operation_timeout_seconds, "browser operation timeout"),
        )
        with self._lock:
            current = (
                self._idle_timeout_seconds,
                self._headless,
                self._channel,
                self._startup_timeout_seconds,
                self._operation_timeout_seconds,
            )
            if self._configured:
                if normalized != current:
                    raise BrowserRuntimeError(
                        "browser_runtime_config_conflict",
                        "browser runtime configuration conflicts with active settings",
                    )
                return
            if self._workers:
                raise BrowserRuntimeError(
                    "browser_runtime_config_conflict",
                    "browser runtime configuration cannot change after startup",
                )
            (
                self._idle_timeout_seconds,
                self._headless,
                self._channel,
                self._startup_timeout_seconds,
                self._operation_timeout_seconds,
            ) = normalized
            self._configured = True

    def get_worker(
        self,
        session_key: str,
        *,
        workspace_root: str | Path | None = None,
        require_workspace_root: bool = False,
    ) -> BrowserWorker:
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
            # 只有首次创建或失效重建时才读取 backend 当前工作目录。
            if require_workspace_root and (
                not isinstance(workspace_root, (str, Path))
                or not str(workspace_root).strip()
            ):
                raise ValueError("browser workspace_root is required for a new session")
            resolved_workspace = self._resolve_workspace_root(workspace_root)
            artifact_dir_config = self._session_artifact_dir(normalized_key)
            worker = BrowserWorker(
                normalized_key,
                workspace_root=resolved_workspace,
                artifact_root=self._artifact_root,
                artifact_dir=artifact_dir_config,
                headless=self._headless,
                channel=self._channel,
                startup_timeout_seconds=self._startup_timeout_seconds,
                operation_timeout_seconds=self._operation_timeout_seconds,
                on_failure=self._remove_failed_worker,
            )
            if worker.failed:
                worker._close_async()
                raise BrowserRuntimeError(
                    worker.failure_error_type,
                    worker.failure_message or "browser worker startup failed",
                )
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
        stale: list[str] = []
        workers: list[BrowserWorker] = []
        for key, worker in self._workers.items():
            if worker.failed:
                stale.append(key)
                workers.append(worker)
            elif worker.close_if_idle(
                now=now, idle_timeout_seconds=self._idle_timeout_seconds
            ):
                stale.append(key)
                workers.append(worker)
        for key in stale:
            self._workers.pop(key, None)
        # 不能持有 Manager 锁等待线程退出；close 的 join 在锁外完成。
        if workers:
            threading.Thread(
                target=lambda: [worker.close() for worker in workers],
                name="browser-idle-cleanup",
                daemon=True,
            ).start()
        return len(workers)

    def _resolve_workspace_root(
        self,
        workspace_root: str | Path | None,
    ) -> Path:
        raw = workspace_root if workspace_root is not None else self._workspace_root
        if raw is None:
            return Path.cwd().resolve()
        resolved = Path(raw).resolve()
        if not resolved.is_dir():
            raise ValueError("browser workspace_root must be an existing directory")
        return resolved

    def _session_artifact_dir(self, session_key: str) -> Path:
        """返回 download 与 screenshot 的共同根目录配置。"""
        return Path(".")

    def _remove_failed_worker(self, session_key: str, worker: BrowserWorker) -> None:
        with self._lock:
            if self._workers.get(session_key) is worker:
                self._workers.pop(session_key, None)

    def _reap_idle_workers(self) -> None:
        """没有新请求时也按固定节奏回收长期空闲的会话。"""
        while not self._stop_reaper.is_set():
            with self._lock:
                interval = min(60.0, max(1.0, self._idle_timeout_seconds / 4))
            if self._stop_reaper.wait(interval):
                return
            try:
                self.cleanup_idle()
            except Exception:
                # 清理器不能因单个会话异常退出，下一轮仍继续回收。
                pass


default_browser_manager = BrowserManager(_allow_reconfigure_once=True)


def configure_default_browser_manager(
    *,
    idle_timeout_seconds: float,
    headless: bool,
    channel: str | None,
    startup_timeout_seconds: float,
    operation_timeout_seconds: float,
) -> None:
    """由 Hermes 工具适配层在启动前固定默认 Manager 的可信配置。"""
    default_browser_manager.configure_once(
        idle_timeout_seconds=idle_timeout_seconds,
        headless=headless,
        channel=channel,
        startup_timeout_seconds=startup_timeout_seconds,
        operation_timeout_seconds=operation_timeout_seconds,
    )


atexit.register(default_browser_manager.shutdown)
