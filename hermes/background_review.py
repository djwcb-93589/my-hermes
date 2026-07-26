"""独立执行已领取的后台记忆审视任务。"""

from __future__ import annotations

import copy
import asyncio
import json
import logging
import math
import threading
from collections.abc import Mapping
from dataclasses import dataclass

from hermes.agent_loop import AgentLoop, AgentLoopResult, _short_error
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
from hermes.persistence.core import get_last_session_message_id
from hermes.persistence.schema import init_db
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
        if self._is_cancelled():
            tool_name = self._tool_call_name(tool_call)
            return (
                f"(error: tool '{tool_name}' cancelled because review claim expired)",
                "cancelled",
                "background review claim expired before tool dispatch",
            )
        tool_name = self._tool_call_name(tool_call)
        if tool_name not in self.allowed_tool_names:
            return (
                f"(error: tool '{tool_name}' is disabled in this review)",
                "disabled",
                f"disabled tool invoked in background review: {tool_name!r}",
            )
        return super().dispatch_one(tool_call)

    def _classify_tool_error(
        self,
        output: str,
        err_status: str | None,
    ) -> tuple[bool, str]:
        """把后台记忆工具的明确错误全部提升为本次审视失败。"""
        fatal, error_type = super()._classify_tool_error(output, err_status)
        if fatal:
            return fatal, error_type
        if err_status:
            return True, error_type or err_status

        payload = None
        if isinstance(output, str):
            try:
                payload = json.loads(output)
            except (TypeError, ValueError):
                pass
        if isinstance(payload, dict):
            payload_error_type = payload.get("error_type")
            if (
                payload.get("ok") is False
                or "error" in payload
                or bool(payload_error_type)
            ):
                return True, str(payload_error_type or "tool_error")
        if isinstance(output, str) and output.lstrip().lower().startswith("(error:"):
            return True, "tool_error"
        return False, error_type

    def process_tool_calls(
        self,
        tool_calls,
        messages,
    ):
        """按顺序处理审视工具；首个明确错误后仅补齐协议消息。"""
        tool_messages: list[dict] = []
        fatal_detail: str | None = None
        fatal_error_type: str | None = None
        skip_remaining = False

        for tool_call in tool_calls:
            tool_name = self._tool_call_name(tool_call)
            if skip_remaining:
                output = "(error: skipped because an earlier background review tool failed)"
            else:
                try:
                    output, err_status, err_detail = self.dispatch_one(tool_call)
                except Exception as exc:
                    short = _short_error(exc)
                    output = f"(error: tool {tool_name} failed: {short})"
                    err_status = "dispatch"
                    err_detail = (
                        f"tool {tool_name!r} dispatch raised: {short}"
                    )

            tool_msg = {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            }
            messages.append(tool_msg)
            tool_messages.append(tool_msg)
            self.on_tool_message(tool_call, tool_msg, output)

            if skip_remaining:
                continue
            if tool_name not in self.tools_used:
                self.tools_used.append(tool_name)

            fatal, error_type = self._classify_tool_error(output, err_status)
            if fatal:
                fatal_detail = (
                    err_detail
                    or f"fatal tool error ({error_type}) in {tool_name!r}"
                )
                fatal_error_type = error_type or "tool_error"
                skip_remaining = True
                continue

            if not error_type and not err_status:
                self._clear_tool_error_counts(tool_name)

        if fatal_detail is not None:
            return tool_messages, self._result(
                ok=False,
                status="tool_error",
                summary=self.last_assistant_text(messages),
                messages=messages,
                error=fatal_detail,
                error_type=fatal_error_type or "tool_error",
                fatal=True,
                retryable=False,
            )
        return tool_messages, None


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
            messages_snapshot = copy.deepcopy(result.messages)
            self._get_executor().submit(
                claim=claim,
                messages_snapshot=messages_snapshot,
            )
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
            def copy_and_submit() -> bool:
                messages_snapshot = copy.deepcopy(result.messages)
                return self._get_executor().submit(
                    claim=claim,
                    messages_snapshot=messages_snapshot,
                )

            await asyncio.to_thread(copy_and_submit)
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
