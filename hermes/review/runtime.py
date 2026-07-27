"""通过 Review Driver 注册表协调和执行后台审视。"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from dataclasses import dataclass

from hermes.agent_loop import AgentLoopResult
from hermes.config import (
    BACKGROUND_REVIEW_CONFIG,
    DB_PATH,
    MODEL,
    MODEL_MAX_OUTPUT_TOKENS,
    client as _default_client,
)
from hermes.persistence.schema import init_db
from hermes.review.contracts import (
    ForegroundReviewEvent,
    ReviewClaim,
    ReviewDriver,
)
from hermes.review.loop import ReviewAgentLoop
from hermes.review.registry import ReviewDriverRegistry
from hermes.tools import registry, register_all


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackgroundReviewConfig:
    """后台审视任务的执行上限与失败冷却规则。"""

    max_iterations: int = 8
    retry_cooldown_seconds: float = 60.0
    max_concurrent_jobs: int = 1

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
        register_all(self.registry)

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
        rejected_for_capacity = False
        with self._lock:
            if self._active_jobs >= self.config.max_concurrent_jobs:
                rejected_for_capacity = True
            else:
                self._active_jobs += 1
        if rejected_for_capacity:
            logger.warning("background review concurrency limit reached")
            self._fail_claim_safely(driver, claim, "review_concurrency_limit")
            return False
        worker = threading.Thread(
            target=self._run_worker,
            args=(driver, claim),
            name=f"background-review-{claim.session_id}",
            daemon=True,
        )
        try:
            worker.start()
        except Exception as exc:
            with self._lock:
                self._active_jobs -= 1
            logger.warning(
                "background review worker could not start: %s",
                type(exc).__name__,
            )
            self._fail_claim_safely(driver, claim, "review_worker_start_failed")
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

    def _run_worker(self, driver: ReviewDriver, claim: ReviewClaim) -> None:
        conn = None
        try:
            conn = init_db(self.db_path)
            if not driver.claim_is_valid(conn, claim):
                logger.debug("background review worker lost its claim before loading")
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
                cancel_checker=lambda: not driver.claim_is_valid(conn, claim),
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
            with self._lock:
                self._active_jobs -= 1

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
    """显式装配当前唯一启用的 Memory Review Driver。"""
    from hermes.review.memory import MemoryReviewDriver
    from hermes.review.memory_store import MemoryReviewStore

    config = BackgroundReviewConfig(
        max_iterations=BACKGROUND_REVIEW_CONFIG["max_iterations"],
        retry_cooldown_seconds=BACKGROUND_REVIEW_CONFIG[
            "retry_cooldown_seconds"
        ],
        max_concurrent_jobs=BACKGROUND_REVIEW_CONFIG["max_concurrent_jobs"],
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
