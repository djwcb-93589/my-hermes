"""通过 Review Driver 注册表协调和执行后台审视。"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum

from hermes.agent_loop import AgentLoopResult
from hermes.config import (
    BACKGROUND_REVIEW_CONFIG,
    DB_PATH,
    MODEL,
    MODEL_MAX_OUTPUT_TOKENS,
    client as _default_client,
)
from hermes.observability.runtime import (
    RuntimeComponentState,
    RuntimeProbeResult,
)
from hermes.persistence.schema import init_db
from hermes.review.contracts import (
    ForegroundReviewEvent,
    ReviewClaim,
    ReviewDriver,
    ReviewKind,
)
from hermes.review.loop import ReviewAgentLoop
from hermes.review.registry import ReviewDriverRegistry
from hermes.tools import registry, register_all


logger = logging.getLogger(__name__)
_BACKGROUND_REVIEW_SHUTDOWN_WAIT_SECONDS = 10.0


class _QueuedClaimValidation(Enum):
    """队列任务在启动前的 claim 校验结果。"""

    VALID = "valid"
    INVALID = "invalid"
    VALIDATION_ERROR = "validation_error"


@dataclass(frozen=True)
class BackgroundReviewConfig:
    """后台审视任务的执行上限与失败冷却规则。"""

    max_iterations: int = 8
    retry_cooldown_seconds: float = 60.0
    max_concurrent_jobs: int = 1
    max_pending_jobs: int = 32

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_iterations, bool)
            or not isinstance(self.max_iterations, int)
            or self.max_iterations <= 0
        ):
            raise ValueError("max_iterations must be a positive integer")
        if isinstance(self.retry_cooldown_seconds, bool):
            raise ValueError("retry_cooldown_seconds must be non-negative")
        try:
            cooldown = float(self.retry_cooldown_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("retry_cooldown_seconds must be non-negative") from exc
        if not math.isfinite(cooldown) or cooldown < 0:
            raise ValueError("retry_cooldown_seconds must be non-negative")
        if (
            isinstance(self.max_concurrent_jobs, bool)
            or not isinstance(self.max_concurrent_jobs, int)
            or self.max_concurrent_jobs <= 0
        ):
            raise ValueError("max_concurrent_jobs must be a positive integer")
        if (
            isinstance(self.max_pending_jobs, bool)
            or not isinstance(self.max_pending_jobs, int)
            or self.max_pending_jobs < 0
        ):
            raise ValueError("max_pending_jobs must be a non-negative integer")


class BackgroundReviewCoordinator:
    """把前台运行事件交给已注册的 Review Driver。"""

    def __init__(
        self,
        *,
        driver_registry: ReviewDriverRegistry,
        executor: "BackgroundReviewExecutor",
        enabled: bool,
    ):
        self.driver_registry = driver_registry
        self.executor = executor
        self.enabled = enabled

    def runtime_probe(self) -> RuntimeProbeResult:
        """返回 Coordinator 与 Executor 的基础设施摘要。"""
        result = self.executor.runtime_probe()
        return RuntimeProbeResult(
            state=result.state,
            metadata={
                **result.metadata,
                "enabled": self.enabled,
                "phase": "ready",
            },
        )

    def shutdown(
        self,
        timeout_seconds: float = _BACKGROUND_REVIEW_SHUTDOWN_WAIT_SECONDS,
    ) -> int:
        """停止接收并有限等待 Background Review worker 收口。"""
        return self.executor.shutdown(timeout_seconds)

    def _fail_submission(
        self,
        conn,
        driver: ReviewDriver,
        claim: ReviewClaim,
        error: str,
    ) -> None:
        """仅在提交异常时释放仍属于当前前台调用的领取。"""
        try:
            released = driver.fail(conn, claim, error)
            if not released:
                logger.debug("background review submission lost its claim")
        except Exception as exc:
            logger.warning(
                "background review could not release submission claim: %s",
                type(exc).__name__,
            )

    def after_foreground_result(
        self,
        conn,
        session_id: str,
        result: AgentLoopResult,
    ) -> None:
        """记录前台事件，并在完成时尽力提交后台审视。"""
        if not self.enabled:
            return
        event = ForegroundReviewEvent(
            session_id=session_id,
            completed=result.ok and result.status == "completed",
            tool_batches=result.tool_batches,
        )
        for driver in self.driver_registry.enabled_drivers():
            try:
                driver.record_progress(conn, event)
            except Exception as exc:
                logger.warning(
                    "background review could not record foreground progress: %s",
                    type(exc).__name__,
                )
                continue
            if not event.completed:
                continue
            try:
                claim = driver.claim_due(conn, session_id)
            except Exception as exc:
                logger.warning(
                    "background review could not claim foreground progress: %s",
                    type(exc).__name__,
                )
                continue
            if claim is None:
                continue
            try:
                self.executor.submit(claim=claim)
            except Exception as exc:
                logger.warning(
                    "background review could not submit foreground claim: %s",
                    type(exc).__name__,
                )
                self._fail_submission(
                    conn,
                    driver,
                    claim,
                    "review_submit_failed",
                )

    async def _persist_async(
        self,
        persistence_call,
        conn,
        operation,
        *args,
        **kwargs,
    ):
        if persistence_call is not None:
            return await persistence_call(operation, *args, **kwargs)
        return operation(conn, *args, **kwargs)

    async def _fail_submission_async(
        self,
        persistence_call,
        conn,
        driver: ReviewDriver,
        claim: ReviewClaim,
        error: str,
    ) -> None:
        try:
            released = await self._persist_async(
                persistence_call,
                conn,
                driver.fail,
                claim,
                error,
            )
            if not released:
                logger.debug("background review submission lost its claim")
        except Exception as exc:
            logger.warning(
                "background review could not release submission claim: %s",
                type(exc).__name__,
            )

    async def after_foreground_result_async(
        self,
        conn,
        session_id: str,
        result: AgentLoopResult,
        *,
        persistence_call=None,
    ) -> None:
        """通过 Gateway 的持久化边界记录并提交后台审视。"""
        if not self.enabled:
            return
        event = ForegroundReviewEvent(
            session_id=session_id,
            completed=result.ok and result.status == "completed",
            tool_batches=result.tool_batches,
        )
        for driver in self.driver_registry.enabled_drivers():
            try:
                await self._persist_async(
                    persistence_call,
                    conn,
                    driver.record_progress,
                    event,
                )
            except Exception as exc:
                logger.warning(
                    "background review could not record foreground progress: %s",
                    type(exc).__name__,
                )
                continue
            if not event.completed:
                continue
            try:
                claim = await self._persist_async(
                    persistence_call,
                    conn,
                    driver.claim_due,
                    session_id,
                )
            except Exception as exc:
                logger.warning(
                    "background review could not claim foreground progress: %s",
                    type(exc).__name__,
                )
                continue
            if claim is None:
                continue
            try:
                await asyncio.to_thread(self.executor.submit, claim=claim)
            except Exception as exc:
                logger.warning(
                    "background review could not submit foreground claim: %s",
                    type(exc).__name__,
                )
                await self._fail_submission_async(
                    persistence_call,
                    conn,
                    driver,
                    claim,
                    "review_submit_failed",
                )


class BackgroundReviewExecutor:
    """进程内后台执行器；每个 worker 使用自己的数据库连接。"""

    def __init__(
        self,
        *,
        driver_registry: ReviewDriverRegistry,
        config: BackgroundReviewConfig,
        model: str = MODEL,
        client=_default_client,
        db_path: str = DB_PATH,
        tool_registry=registry,
    ):
        self.driver_registry = driver_registry
        self.config = config
        self.model = model
        self.client = client
        self.db_path = db_path
        self.registry = tool_registry
        self._lock = threading.Lock()
        self._active_jobs = 0
        self._pending_jobs: deque[ReviewClaim] = deque()
        self._scheduled_claims: set[tuple[ReviewKind, str, str]] = set()
        self._worker_threads: set[threading.Thread] = set()
        self._accepting = True
        self._shutdown_event = threading.Event()
        register_all(self.registry)

    def runtime_probe(self) -> RuntimeProbeResult:
        """返回执行器的有限队列摘要，不读取 Review 内容或领取身份。"""
        with self._lock:
            worker_count = self._active_jobs
            queue_depth = len(self._pending_jobs)
        return RuntimeProbeResult(
            state=(
                RuntimeComponentState.RUNNING
                if worker_count
                else RuntimeComponentState.IDLE
            ),
            metadata={
                "queue_depth": queue_depth,
                "worker_count": worker_count,
            },
        )

    def submit(self, *, claim: ReviewClaim) -> bool:
        """提交已领取的审视任务，并立即返回。"""
        if not self._valid_claim_identity(claim):
            logger.warning("background review rejected an invalid claim identity")
            return False
        driver = self.driver_registry.get(claim.kind)
        if driver is None:
            logger.warning("background review rejected a claim with no registered driver")
            return False
        if not driver.validate_claim(claim):
            logger.warning("background review rejected an invalid review claim")
            self._fail_claim_safely(
                driver,
                claim,
                "invalid_or_unsupported_review_claim",
            )
            return False
        claim_key = self._claim_key(claim)
        start_immediately = False
        queue_full = False
        shutting_down = False
        with self._lock:
            if not self._accepting:
                shutting_down = True
            elif claim_key in self._scheduled_claims:
                logger.debug("background review claim is already scheduled")
                return True
            elif self._pending_jobs:
                if len(self._pending_jobs) < self.config.max_pending_jobs:
                    self._pending_jobs.append(claim)
                    self._scheduled_claims.add(claim_key)
                    logger.debug("background review claim queued")
                    return True
                queue_full = True
            elif self._active_jobs < self.config.max_concurrent_jobs:
                self._active_jobs += 1
                self._scheduled_claims.add(claim_key)
                start_immediately = True
            elif len(self._pending_jobs) < self.config.max_pending_jobs:
                self._pending_jobs.append(claim)
                self._scheduled_claims.add(claim_key)
                logger.debug("background review claim queued")
                return True
            else:
                queue_full = True
        if shutting_down:
            self._fail_claim_safely(
                driver,
                claim,
                "review_executor_shutting_down",
            )
            return False
        if queue_full:
            logger.warning("background review queue is full")
            self._fail_claim_safely(driver, claim, "review_queue_full")
            return False
        if not start_immediately:
            return False
        if not self._start_reserved_worker(driver, claim):
            self._start_next_pending_job()
            return False
        logger.debug("background review submitted")
        return True

    @staticmethod
    def _valid_claim_identity(claim: object) -> bool:
        """校验能够安全释放领取所需的最小身份信息。"""
        return (
            isinstance(claim, ReviewClaim)
            and isinstance(claim.session_id, str)
            and bool(claim.session_id.strip())
            and isinstance(claim.token, str)
            and bool(claim.token)
        )

    @staticmethod
    def _claim_key(claim: ReviewClaim) -> tuple[ReviewKind, str, str]:
        """构造进程内调度去重所需的稳定 claim 身份。"""
        return claim.kind, claim.session_id, claim.token

    def _release_reserved_claim(self, claim: ReviewClaim) -> None:
        """释放已经占用的并发槽位，不执行数据库操作。"""
        with self._lock:
            self._release_reserved_claim_locked(claim)

    def _release_reserved_claim_locked(self, claim: ReviewClaim) -> None:
        """在执行器锁内释放已经预留的并发槽位。"""
        claim_key = self._claim_key(claim)
        if claim_key not in self._scheduled_claims:
            return
        self._active_jobs -= 1
        self._scheduled_claims.discard(claim_key)

    def _start_reserved_worker(
        self,
        driver: ReviewDriver,
        claim: ReviewClaim,
    ) -> bool:
        """启动已预留并发槽位的 worker；失败时只释放当前 claim。"""
        try:
            worker = threading.Thread(
                target=self._run_worker,
                args=(driver, claim),
                name=f"background-review-{claim.session_id}",
                daemon=True,
            )
        except Exception as exc:
            self._release_reserved_claim(claim)
            logger.warning(
                "background review worker could not start: %s",
                type(exc).__name__,
            )
            self._fail_claim_safely(driver, claim, "review_worker_start_failed")
            return False
        rejected = False
        start_error: Exception | None = None
        with self._lock:
            if not self._accepting:
                self._release_reserved_claim_locked(claim)
                rejected = True
            else:
                self._worker_threads = {
                    thread
                    for thread in self._worker_threads
                    if thread.is_alive()
                }
                self._worker_threads.add(worker)
                try:
                    worker.start()
                except Exception as exc:
                    self._worker_threads.discard(worker)
                    self._release_reserved_claim_locked(claim)
                    start_error = exc
        if rejected:
            self._fail_claim_safely(
                driver,
                claim,
                "review_executor_shutting_down",
            )
            return False
        if start_error is not None:
            logger.warning(
                "background review worker could not start: %s",
                type(start_error).__name__,
            )
            self._fail_claim_safely(driver, claim, "review_worker_start_failed")
            return False
        return True

    def _reserve_next_pending_claim(self) -> ReviewClaim | None:
        """按 FIFO 顺序取出下一项，并在锁内预留一个并发槽位。"""
        with self._lock:
            if (
                not self._accepting
                or self._active_jobs >= self.config.max_concurrent_jobs
                or not self._pending_jobs
            ):
                return None
            claim = self._pending_jobs.popleft()
            self._active_jobs += 1
            return claim

    def _validate_queued_claim(
        self,
        driver: ReviewDriver,
        claim: ReviewClaim,
    ) -> _QueuedClaimValidation:
        """在队列等待后用独立连接确认 claim 未失效。"""
        conn = None
        try:
            conn = init_db(self.db_path)
            if driver.claim_is_valid(conn, claim):
                return _QueuedClaimValidation.VALID
            return _QueuedClaimValidation.INVALID
        except Exception as exc:
            logger.warning(
                "background review queued claim validation failed: %s",
                type(exc).__name__,
                exc_info=True,
            )
            return _QueuedClaimValidation.VALIDATION_ERROR
        finally:
            if conn is not None:
                conn.close()

    def _start_next_pending_job(self) -> None:
        """启动最早的有效等待任务；失效任务不会阻塞后续 claim。"""
        while True:
            claim = self._reserve_next_pending_claim()
            if claim is None:
                return
            driver = self.driver_registry.get(claim.kind)
            if driver is None:
                logger.warning("background review dropped a queued claim with no driver")
                self._release_reserved_claim(claim)
                continue
            validation = self._validate_queued_claim(driver, claim)
            if validation is _QueuedClaimValidation.INVALID:
                logger.debug("background review dropped an invalid queued claim")
                self._release_reserved_claim(claim)
                continue
            if validation is _QueuedClaimValidation.VALIDATION_ERROR:
                self._release_reserved_claim(claim)
                self._fail_claim_safely(
                    driver,
                    claim,
                    "review_claim_validation_failed",
                )
                continue
            if self._start_reserved_worker(driver, claim):
                return

    def _run_worker(self, driver: ReviewDriver, claim: ReviewClaim) -> None:
        conn = None
        try:
            conn = init_db(self.db_path)
            if not driver.claim_is_valid(conn, claim):
                logger.debug("background review worker lost its claim before loading")
                return
            if self._shutdown_event.is_set():
                self._fail_claim(
                    driver,
                    conn,
                    claim,
                    "review_executor_shutting_down",
                )
                return
            try:
                run_spec = driver.prepare_run(conn, claim)
            except Exception as exc:
                logger.warning(
                    "background review could not prepare review run: %s",
                    type(exc).__name__,
                )
                if driver.claim_is_valid(conn, claim):
                    self._fail_claim(driver, conn, claim, "review_prepare_failed")
                return
            if not driver.claim_is_valid(conn, claim):
                logger.debug("background review worker lost its claim after loading")
                return
            if self._shutdown_event.is_set():
                self._fail_claim(
                    driver,
                    conn,
                    claim,
                    "review_executor_shutting_down",
                )
                return
            resolution = self.registry.resolve(run_spec.tool_policy)
            if not resolution.definitions:
                self._fail_claim(driver, conn, claim, "review_tools_unavailable")
                return
            loop = ReviewAgentLoop(
                review_messages=run_spec.messages,
                review_instruction=run_spec.instruction,
                allowed_tool_names=resolution.allowed_tool_names,
                model=self.model,
                max_iterations=run_spec.max_iterations,
                tools=list(resolution.definitions),
                system_prompt=run_spec.system_prompt,
                registry=self.registry,
                client=self.client,
                session_key=claim.session_id,
                model_kwargs={"max_tokens": MODEL_MAX_OUTPUT_TOKENS},
                cancel_checker=lambda: (
                    self._shutdown_event.is_set()
                    or not driver.claim_is_valid(conn, claim)
                ),
                tool_context=run_spec.tool_context,
            )
            result = loop.run("")
            if result.ok and result.status == "completed":
                if not driver.complete(conn, claim):
                    logger.debug("background review completion lost its claim")
                else:
                    logger.debug("background review completed")
                return
            self._fail_claim(
                driver,
                conn,
                claim,
                f"review_failed:{result.status}:{result.error_type or 'unknown'}",
            )
        except Exception as exc:
            logger.warning("background review worker failed: %s", type(exc).__name__)
            if conn is None:
                self._fail_claim_safely(driver, claim, "review_worker_failed")
            else:
                self._fail_claim(driver, conn, claim, "review_worker_failed")
        finally:
            if conn is not None:
                conn.close()
            self._release_reserved_claim(claim)
            self._start_next_pending_job()

    def shutdown(
        self,
        timeout_seconds: float = _BACKGROUND_REVIEW_SHUTDOWN_WAIT_SECONDS,
    ) -> int:
        """停止接收、释放排队领取并有限等待已启动 worker。"""
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
        ):
            raise ValueError("timeout_seconds must be a non-negative number")
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout_seconds must be a non-negative number")

        with self._lock:
            self._accepting = False
            self._shutdown_event.set()
            pending_claims = tuple(self._pending_jobs)
            self._pending_jobs.clear()
            for claim in pending_claims:
                self._scheduled_claims.discard(self._claim_key(claim))
            workers = tuple(self._worker_threads)

        for claim in pending_claims:
            driver = self.driver_registry.get(claim.kind)
            if driver is None:
                continue
            self._fail_claim_safely(
                driver,
                claim,
                "review_executor_shutting_down",
            )

        deadline = time.monotonic() + timeout
        current_thread = threading.current_thread()
        for worker in workers:
            if worker is current_thread or not worker.is_alive():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(remaining)
        # 当前线程无法 join 自身，但只要仍存活就必须计入未完成结果，
        # 避免调用方过早宣称 Executor 已经停止。
        return sum(worker.is_alive() for worker in workers)

    def _fail_claim(
        self,
        driver: ReviewDriver,
        conn,
        claim: ReviewClaim,
        error: str,
    ) -> None:
        try:
            if not driver.fail(conn, claim, error):
                logger.debug("background review failure lost its claim")
        except Exception as exc:
            logger.warning(
                "background review could not release claim: %s",
                type(exc).__name__,
            )

    def _fail_claim_safely(
        self,
        driver: ReviewDriver,
        claim: ReviewClaim,
        error: str,
    ) -> None:
        conn = None
        try:
            conn = init_db(self.db_path)
            self._fail_claim(driver, conn, claim, error)
        except Exception as exc:
            logger.warning(
                "background review could not open a connection to release claim: %s",
                type(exc).__name__,
            )
        finally:
            if conn is not None:
                conn.close()


_coordinator_lock = threading.Lock()
_background_review_coordinator: BackgroundReviewCoordinator | None = None


def _build_default_coordinator() -> BackgroundReviewCoordinator:
    """显式装配默认启用的 Review Driver。"""
    from hermes.review.memory import MemoryReviewDriver
    from hermes.review.memory_store import MemoryReviewStore

    config = BackgroundReviewConfig(
        max_iterations=BACKGROUND_REVIEW_CONFIG["max_iterations"],
        retry_cooldown_seconds=BACKGROUND_REVIEW_CONFIG[
            "retry_cooldown_seconds"
        ],
        max_concurrent_jobs=BACKGROUND_REVIEW_CONFIG["max_concurrent_jobs"],
        max_pending_jobs=BACKGROUND_REVIEW_CONFIG["max_pending_jobs"],
    )
    driver_registry = ReviewDriverRegistry()
    memory_driver = MemoryReviewDriver(
        store=MemoryReviewStore(),
        memory_interval=BACKGROUND_REVIEW_CONFIG["memory_interval"],
        claim_ttl_seconds=BACKGROUND_REVIEW_CONFIG["claim_ttl_seconds"],
        retry_cooldown_seconds=config.retry_cooldown_seconds,
        max_iterations=config.max_iterations,
    )
    driver_registry.register(memory_driver)
    if BACKGROUND_REVIEW_CONFIG["skill_tool_batch_interval"] > 0:
        try:
            from hermes.review.skill import SkillReviewDriver
            from hermes.review.skill_store import SkillReviewStore

            driver_registry.register(
                SkillReviewDriver(
                    store=SkillReviewStore(),
                    skill_tool_batch_interval=BACKGROUND_REVIEW_CONFIG[
                        "skill_tool_batch_interval"
                    ],
                    claim_ttl_seconds=BACKGROUND_REVIEW_CONFIG[
                        "claim_ttl_seconds"
                    ],
                    retry_cooldown_seconds=config.retry_cooldown_seconds,
                    max_iterations=config.max_iterations,
                )
            )
        except Exception as exc:
            logger.warning(
                "Skill Review driver unavailable; Skill Review was skipped: %s",
                type(exc).__name__,
                exc_info=True,
            )
    executor = BackgroundReviewExecutor(
        driver_registry=driver_registry,
        config=config,
    )
    return BackgroundReviewCoordinator(
        driver_registry=driver_registry,
        executor=executor,
        enabled=BACKGROUND_REVIEW_CONFIG["enabled"] is True,
    )


def get_background_review_coordinator() -> BackgroundReviewCoordinator:
    """返回进程共享的惰性后台审视协调器。"""
    global _background_review_coordinator
    with _coordinator_lock:
        if _background_review_coordinator is None:
            _background_review_coordinator = _build_default_coordinator()
        return _background_review_coordinator


def probe_background_review_runtime() -> RuntimeProbeResult:
    """探测惰性 Coordinator，不为 Heartbeat 提前初始化模型或工具依赖。"""
    with _coordinator_lock:
        coordinator = _background_review_coordinator
    if coordinator is None:
        enabled = BACKGROUND_REVIEW_CONFIG["enabled"] is True
        return RuntimeProbeResult(
            state=RuntimeComponentState.IDLE,
            metadata={
                "enabled": enabled,
                "phase": "lazy" if enabled else "inactive",
                "queue_depth": 0,
                "worker_count": 0,
            },
        )
    return coordinator.runtime_probe()


def shutdown_background_review_runtime(
    timeout_seconds: float = _BACKGROUND_REVIEW_SHUTDOWN_WAIT_SECONDS,
) -> int:
    """关闭已初始化 Coordinator；未初始化时不创建任何运行时对象。"""
    global _background_review_coordinator
    with _coordinator_lock:
        coordinator = _background_review_coordinator
    if coordinator is None:
        return 0
    unfinished_workers = coordinator.shutdown(timeout_seconds)
    if unfinished_workers == 0:
        with _coordinator_lock:
            if _background_review_coordinator is coordinator:
                _background_review_coordinator = None
    return unfinished_workers
