"""独立执行已领取的后台记忆审视任务。"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from collections.abc import Mapping
from dataclasses import dataclass

from hermes.agent_loop import AgentLoopResult
from hermes.config import (
    BACKGROUND_REVIEW_CONFIG,
    DB_PATH,
    MODEL,
    MODEL_MAX_OUTPUT_TOKENS,
    client as _default_client,
)
from hermes.persistence.background_review import (
    background_review_claim_is_valid,
    claim_due_background_review,
    complete_background_review_claim,
    fail_background_review_claim,
    record_background_review_progress,
)
from hermes.persistence.core import (
    get_last_session_message_id,
    get_session_messages_in_id_range,
)
from hermes.persistence.schema import init_db
from hermes.review.loop import ReviewAgentLoop
from hermes.tools import (
    ApprovalMode,
    ExecutionEnvironment,
    ToolPolicy,
    ToolRiskLevel,
    registry,
    register_all,
)


logger = logging.getLogger(__name__)


REVIEW_SYSTEM_PROMPT = (
    "You review only the newly added dialog since the last completed review, not "
    "the full conversation. Earlier dialog was already handled: do not invent or "
    "re-extract information from it. Use only the provided memory tools. Before "
    "creating, replacing, or deleting persisted memory, inspect the current live "
    "stored memory through those tools. Compare it with existing user information "
    "and long-term memory to avoid semantic duplicates. If the information is "
    "already present, do not write it; if it supplements or corrects existing "
    "information, update it instead of creating a duplicate. Retain only stable "
    "preferences, context, explicit requirements, and facts likely to be useful "
    "later. Do not retain temporary task progress, one-off requests, easily "
    "rediscovered information, tool-output details, or unconfirmed inferences. "
    "If nothing is worth retaining, reply exactly: Nothing to save"
)


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
    """在前台结果与后台审视执行器之间协调进度和任务领取。"""

    def __init__(
        self,
        config: Mapping[str, object] = BACKGROUND_REVIEW_CONFIG,
        *,
        executor: "BackgroundReviewExecutor | None" = None,
    ):
        self.config = config
        self._executor = executor
        self._executor_lock = threading.Lock()

    def _get_executor(self) -> "BackgroundReviewExecutor":
        """按需创建进程内唯一的默认执行器。"""
        with self._executor_lock:
            if self._executor is None:
                self._executor = BackgroundReviewExecutor(
                    BackgroundReviewConfig(
                        max_iterations=self.config["max_iterations"],
                        retry_cooldown_seconds=self.config[
                            "retry_cooldown_seconds"
                        ],
                        max_concurrent_jobs=self.config[
                            "max_concurrent_jobs"
                        ],
                    )
                )
            return self._executor

    def _enabled(self) -> bool:
        return self.config["enabled"] is True

    def _claim(self, conn, session_id: str):
        return claim_due_background_review(
            conn,
            session_id,
            memory_interval=self.config["memory_interval"],
            skill_interval=self.config["skill_interval"],
            claim_ttl_seconds=self.config["claim_ttl_seconds"],
        )

    def _fail_submission(self, conn, claim: dict, error: str) -> None:
        """仅在提交异常时释放仍属于当前前台调用的 claim。"""
        try:
            released = fail_background_review_claim(
                conn,
                claim["session_id"],
                claim["claim_token"],
                error=error,
                retry_cooldown_seconds=self.config["retry_cooldown_seconds"],
            )
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
        *,
        resume_from_history: bool,
    ) -> None:
        """记录一次已启动前台循环，并在完整成功后尽力提交后台审视。"""
        if not self._enabled():
            return
        memory_turns = 0
        memory_message_upto = None
        if result.ok and result.status == "completed":
            try:
                memory_message_upto = get_last_session_message_id(conn, session_id)
            except Exception as exc:
                logger.warning(
                    "background review could not read foreground message boundary: %s",
                    type(exc).__name__,
                )
            else:
                if memory_message_upto is None:
                    logger.warning(
                        "background review skipped memory progress without messages"
                    )
                else:
                    memory_turns = 1
        try:
            record_background_review_progress(
                conn,
                session_id,
                memory_turns=memory_turns,
                memory_message_upto=memory_message_upto,
                skill_tool_batches=result.tool_batches,
            )
        except Exception as exc:
            logger.warning(
                "background review could not record foreground progress: %s",
                type(exc).__name__,
            )
            return

        if not (result.ok and result.status == "completed"):
            return
        try:
            claim = self._claim(conn, session_id)
        except Exception as exc:
            logger.warning(
                "background review could not claim foreground progress: %s",
                type(exc).__name__,
            )
            return
        if claim is None:
            return
        try:
            self._get_executor().submit(claim=claim)
        except Exception as exc:
            logger.warning(
                "background review could not submit foreground claim: %s",
                type(exc).__name__,
            )
            self._fail_submission(conn, claim, "background_review_submit_failed")

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
        claim: dict,
        error: str,
    ) -> None:
        try:
            released = await self._persist_async(
                persistence_call,
                conn,
                fail_background_review_claim,
                claim["session_id"],
                claim["claim_token"],
                error=error,
                retry_cooldown_seconds=self.config["retry_cooldown_seconds"],
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
        resume_from_history: bool,
        persistence_call=None,
    ) -> None:
        """异步入口遵守 Gateway 持久化边界，且不阻塞事件循环提交快照。"""
        if not self._enabled():
            return
        memory_turns = 0
        memory_message_upto = None
        if result.ok and result.status == "completed":
            try:
                memory_message_upto = await self._persist_async(
                    persistence_call,
                    conn,
                    get_last_session_message_id,
                    session_id,
                )
            except Exception as exc:
                logger.warning(
                    "background review could not read foreground message boundary: %s",
                    type(exc).__name__,
                )
            else:
                if memory_message_upto is None:
                    logger.warning(
                        "background review skipped memory progress without messages"
                    )
                else:
                    memory_turns = 1
        try:
            await self._persist_async(
                persistence_call,
                conn,
                record_background_review_progress,
                session_id,
                memory_turns=memory_turns,
                memory_message_upto=memory_message_upto,
                skill_tool_batches=result.tool_batches,
            )
        except Exception as exc:
            logger.warning(
                "background review could not record foreground progress: %s",
                type(exc).__name__,
            )
            return

        if not (result.ok and result.status == "completed"):
            return
        try:
            claim = await self._persist_async(
                persistence_call,
                conn,
                claim_due_background_review,
                session_id,
                memory_interval=self.config["memory_interval"],
                skill_interval=self.config["skill_interval"],
                claim_ttl_seconds=self.config["claim_ttl_seconds"],
            )
        except Exception as exc:
            logger.warning(
                "background review could not claim foreground progress: %s",
                type(exc).__name__,
            )
            return
        if claim is None:
            return
        try:
            def submit_claim() -> bool:
                return self._get_executor().submit(claim=claim)

            await asyncio.to_thread(submit_claim)
        except Exception as exc:
            logger.warning(
                "background review could not submit foreground claim: %s",
                type(exc).__name__,
            )
            await self._fail_submission_async(
                persistence_call,
                conn,
                claim,
                "background_review_submit_failed",
            )


_coordinator_lock = threading.Lock()
_background_review_coordinator: BackgroundReviewCoordinator | None = None


def get_background_review_coordinator() -> BackgroundReviewCoordinator:
    """返回进程共享的惰性后台审视协调器。"""
    global _background_review_coordinator
    with _coordinator_lock:
        if _background_review_coordinator is None:
            _background_review_coordinator = BackgroundReviewCoordinator()
        return _background_review_coordinator


class BackgroundReviewExecutor:
    """进程内后台执行器；每个 worker 使用自己的数据库连接。"""

    def __init__(
        self,
        config: BackgroundReviewConfig,
        *,
        model: str = MODEL,
        client=_default_client,
        db_path: str = DB_PATH,
        tool_registry=registry,
    ):
        self.config = config
        self.model = model
        self.client = client
        self.db_path = db_path
        self.registry = tool_registry
        self._lock = threading.Lock()
        self._active_jobs = 0
        register_all(self.registry)

    def submit(
        self,
        *,
        claim: dict,
    ) -> bool:
        """提交已领取的记忆审视任务，并立即返回。"""
        if not self._valid_claim_identity(claim):
            logger.warning("background review rejected an invalid claim identity")
            return False
        if not self._valid_memory_review_claim(claim):
            logger.warning(
                "background review rejected an invalid or unsupported claim"
            )
            self._fail_claim_safely(
                claim,
                "invalid_or_unsupported_background_review_claim",
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
            self._fail_claim_safely(claim, "background_review_concurrency_limit")
            return False
        worker = threading.Thread(
            target=self._run_worker,
            args=(dict(claim),),
            name=f"background-review-{claim['session_id']}",
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
            self._fail_claim_safely(claim, "background_review_worker_start_failed")
            return False
        logger.debug("background review submitted")
        return True

    @staticmethod
    def _valid_claim_identity(claim: object) -> bool:
        """校验能够安全释放领取所需的最小身份信息。"""
        if not isinstance(claim, dict):
            return False
        return (
            isinstance(claim.get("session_id"), str)
            and bool(claim["session_id"].strip())
            and isinstance(claim.get("claim_token"), str)
            and bool(claim["claim_token"])
        )

    @staticmethod
    def _valid_memory_review_claim(claim: object) -> bool:
        """校验当前 P2 Memory Review 能够执行的完整领取协议。"""
        if not BackgroundReviewExecutor._valid_claim_identity(claim):
            return False
        assert isinstance(claim, dict)
        memory_message_after = claim.get("memory_message_after")
        memory_message_upto = claim.get("memory_message_upto")
        memory_upto = claim.get("memory_upto")
        return (
            claim.get("review_memory") is True
            and claim.get("review_skills") is False
            and not isinstance(memory_message_after, bool)
            and isinstance(memory_message_after, int)
            and memory_message_after >= 0
            and not isinstance(memory_message_upto, bool)
            and isinstance(memory_message_upto, int)
            and memory_message_upto > memory_message_after
            and not isinstance(memory_upto, bool)
            and isinstance(memory_upto, int)
            and memory_upto >= 0
        )

    def _run_worker(self, claim: dict) -> None:
        conn = None
        try:
            conn = init_db(self.db_path)
            if not background_review_claim_is_valid(
                conn,
                claim["session_id"],
                claim["claim_token"],
            ):
                logger.debug("background review worker lost its claim before loading")
                return
            try:
                messages_snapshot = get_session_messages_in_id_range(
                    conn,
                    claim["session_id"],
                    after_message_id=claim["memory_message_after"],
                    upto_message_id=claim["memory_message_upto"],
                )
            except Exception as exc:
                logger.warning(
                    "background review could not load message window: %s",
                    type(exc).__name__,
                )
                self._fail_claim(
                    conn,
                    claim,
                    "background_review_message_window_load_failed",
                )
                return
            if not background_review_claim_is_valid(
                conn,
                claim["session_id"],
                claim["claim_token"],
            ):
                logger.debug("background review worker lost its claim after loading")
                return
            resolution = self.registry.resolve(
                ToolPolicy(
                    ExecutionEnvironment.BACKGROUND_REVIEW,
                    enabled_toolsets=frozenset({"memory"}),
                    unattended=True,
                    allowed_approval_modes=frozenset({ApprovalMode.NONE.value}),
                    max_risk_level=ToolRiskLevel.MEDIUM,
                )
            )
            if not resolution.definitions:
                self._fail_claim(conn, claim, "no_memory_tools_available")
                return
            loop = ReviewAgentLoop(
                review_messages=messages_snapshot,
                review_instruction=REVIEW_SYSTEM_PROMPT,
                allowed_tool_names=resolution.allowed_tool_names,
                model=self.model,
                max_iterations=self.config.max_iterations,
                tools=list(resolution.definitions),
                system_prompt="You are a background memory review agent.",
                registry=self.registry,
                client=self.client,
                session_key=claim["session_id"],
                model_kwargs={"max_tokens": MODEL_MAX_OUTPUT_TOKENS},
                cancel_checker=lambda: not background_review_claim_is_valid(
                    conn, claim["session_id"], claim["claim_token"]
                ),
            )
            result = loop.run("")
            if result.ok and result.status == "completed":
                if not complete_background_review_claim(
                    conn, claim["session_id"], claim["claim_token"]
                ):
                    logger.debug("background review completion lost its claim")
                else:
                    logger.debug("background review completed")
                return
            self._fail_claim(
                conn,
                claim,
                f"review_failed:{result.status}:{result.error_type or 'unknown'}",
            )
        except Exception as exc:
            logger.warning("background review worker failed: %s", type(exc).__name__)
            if conn is None:
                self._fail_claim_safely(claim, "background_review_worker_failed")
            else:
                self._fail_claim(conn, claim, "background_review_worker_failed")
        finally:
            if conn is not None:
                conn.close()
            with self._lock:
                self._active_jobs -= 1

    def _fail_claim(self, conn, claim: dict, error: str) -> None:
        try:
            if not fail_background_review_claim(
                conn,
                claim["session_id"],
                claim["claim_token"],
                error=error,
                retry_cooldown_seconds=self.config.retry_cooldown_seconds,
            ):
                logger.debug("background review failure lost its claim")
        except Exception as exc:
            logger.warning(
                "background review could not release claim: %s",
                type(exc).__name__,
            )

    def _fail_claim_safely(self, claim: dict, error: str) -> None:
        conn = None
        try:
            conn = init_db(self.db_path)
            self._fail_claim(conn, claim, error)
        except Exception as exc:
            logger.warning(
                "background review could not open a connection to release claim: %s",
                type(exc).__name__,
            )
        finally:
            if conn is not None:
                conn.close()
