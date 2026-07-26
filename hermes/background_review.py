"""独立执行已领取的后台记忆审视任务。"""

from __future__ import annotations

import copy
import logging
import math
import threading
from dataclasses import dataclass

from hermes.agent_loop import AgentLoop
from hermes.config import DB_PATH, MODEL, MODEL_MAX_OUTPUT_TOKENS, client as _default_client
from hermes.persistence.background_review import (
    complete_background_review_claim,
    fail_background_review_claim,
)
from hermes.persistence.schema import init_db
from hermes.tools import ExecutionEnvironment, ToolPolicy, registry, register_all


logger = logging.getLogger(__name__)


REVIEW_SYSTEM_PROMPT = (
    "You review a completed conversation for stable information worth retaining "
    "across future conversations. Use only the tools provided to inspect existing "
    "information and perform any appropriate permitted operation. Focus on stable "
    "user preferences, long-term context, explicit behavioral requirements, and "
    "facts likely to remain useful later. Do not retain temporary task progress, "
    "one-off requests, easily rediscovered information, tool-output details, or "
    "unconfirmed inferences. If nothing is worth retaining, reply exactly: "
    "Nothing to save"
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


class ReviewAgentLoop(AgentLoop):
    """只复用通用 AgentLoop 的后台记忆审视循环。"""

    def __init__(
        self,
        *,
        messages_snapshot: list[dict],
        allowed_tool_names: frozenset[str],
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._messages_snapshot = copy.deepcopy(messages_snapshot)
        self.allowed_tool_names = allowed_tool_names

    def init_messages(self, user_message: str) -> list[dict]:
        messages = copy.deepcopy(self._messages_snapshot)
        messages.append({"role": "user", "content": REVIEW_SYSTEM_PROMPT})
        return messages

    def handle_model_error(self, exc, messages) -> str:
        """后台审视不继承主会话的 fallback 或重试策略。"""
        return "abort"

    def dispatch_one(self, tool_call):
        """拒绝不在本次动态解析能力边界内的工具调用。"""
        tool_name = self._tool_call_name(tool_call)
        if tool_name not in self.allowed_tool_names:
            return (
                f"(error: tool '{tool_name}' is disabled in this review)",
                "disabled",
                f"disabled tool invoked in background review: {tool_name!r}",
            )
        return super().dispatch_one(tool_call)


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

    def submit(
        self,
        *,
        claim: dict,
        messages_snapshot: list[dict],
    ) -> bool:
        """提交已领取的记忆审视任务，并立即返回。"""
        if not self._valid_claim(claim):
            logger.warning("background review rejected an invalid claim")
            return False
        if not isinstance(messages_snapshot, list):
            logger.warning("background review rejected an invalid snapshot")
            self._fail_claim_safely(claim, "invalid_messages_snapshot")
            return False
        try:
            snapshot = copy.deepcopy(messages_snapshot)
        except Exception as exc:
            logger.warning(
                "background review could not copy snapshot: %s",
                type(exc).__name__,
            )
            self._fail_claim_safely(claim, "snapshot_copy_failed")
            return False
        if not claim["review_memory"] or claim["review_skills"]:
            logger.warning("background review received an unsupported claim type")
            self._fail_claim_safely(claim, "unsupported_review_claim")
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
            args=(dict(claim), snapshot),
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
    def _valid_claim(claim: dict) -> bool:
        return (
            isinstance(claim, dict)
            and isinstance(claim.get("session_id"), str)
            and bool(claim["session_id"])
            and isinstance(claim.get("claim_token"), str)
            and bool(claim["claim_token"])
            and isinstance(claim.get("review_memory"), bool)
            and isinstance(claim.get("review_skills"), bool)
        )

    def _run_worker(self, claim: dict, messages_snapshot: list[dict]) -> None:
        conn = None
        try:
            conn = init_db(self.db_path)
            register_all()
            resolution = self.registry.resolve(
                ToolPolicy(
                    ExecutionEnvironment.BACKGROUND_REVIEW,
                    enabled_toolsets=frozenset({"memory"}),
                )
            )
            if not resolution.definitions:
                self._fail_claim(conn, claim, "no_memory_tools_available")
                return
            loop = ReviewAgentLoop(
                messages_snapshot=messages_snapshot,
                allowed_tool_names=resolution.allowed_tool_names,
                model=self.model,
                max_iterations=self.config.max_iterations,
                tools=list(resolution.definitions),
                system_prompt="You are a background memory review agent.",
                registry=self.registry,
                client=self.client,
                session_key=claim["session_id"],
                model_kwargs={"max_tokens": MODEL_MAX_OUTPUT_TOKENS},
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
