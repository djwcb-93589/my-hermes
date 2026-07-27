"""当前唯一启用的 Memory Review Driver。"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from hermes.review.contracts import (
    ForegroundReviewEvent,
    ReviewClaim,
    ReviewKind,
    ReviewRunSpec,
)
from hermes.review.memory_store import MemoryReviewStore
from hermes.tools import (
    ApprovalMode,
    ExecutionEnvironment,
    ToolPolicy,
    ToolRiskLevel,
)


logger = logging.getLogger(__name__)


MEMORY_REVIEW_SYSTEM_PROMPT = "You are a background memory review agent."

MEMORY_REVIEW_INSTRUCTION = (
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


class MemoryReviewDriver:
    """将 Memory Review 的进度、领取和运行输入封装为统一契约。"""

    kind = ReviewKind.MEMORY

    def __init__(
        self,
        *,
        store: MemoryReviewStore,
        memory_interval: int,
        claim_ttl_seconds: float,
        retry_cooldown_seconds: float,
        max_iterations: int,
    ):
        self.store = store
        self.memory_interval = memory_interval
        self.claim_ttl_seconds = claim_ttl_seconds
        self.retry_cooldown_seconds = retry_cooldown_seconds
        self.max_iterations = max_iterations

    def record_progress(self, conn, event: ForegroundReviewEvent) -> None:
        completed_turns = 0
        message_upto = None
        if event.completed:
            message_upto = self.store.get_last_message_id(conn, event.session_id)
            if message_upto is None:
                logger.warning("memory review skipped progress without messages")
            else:
                completed_turns = 1
        self.store.record_progress(
            conn,
            event.session_id,
            completed_turns=completed_turns,
            message_upto=message_upto,
        )

    def claim_due(self, conn, session_id: str) -> ReviewClaim | None:
        raw_claim = self.store.claim_due(
            conn,
            session_id,
            memory_interval=self.memory_interval,
            claim_ttl_seconds=self.claim_ttl_seconds,
        )
        if raw_claim is None:
            return None
        if raw_claim.get("review_skills") is True:
            self.store.fail(
                conn,
                raw_claim["session_id"],
                raw_claim["claim_token"],
                error="invalid_or_unsupported_memory_review_claim",
                retry_cooldown_seconds=self.retry_cooldown_seconds,
            )
            raise ValueError("memory review claim includes unsupported skill review")
        if raw_claim.get("review_memory") is not True:
            self.store.fail(
                conn,
                raw_claim["session_id"],
                raw_claim["claim_token"],
                error="invalid_or_unsupported_memory_review_claim",
                retry_cooldown_seconds=self.retry_cooldown_seconds,
            )
            raise ValueError("memory review claim is missing memory review")
        return ReviewClaim(
            kind=ReviewKind.MEMORY,
            session_id=raw_claim["session_id"],
            token=raw_claim["claim_token"],
            payload={
                "turn_upto": raw_claim["memory_upto"],
                "message_after": raw_claim["memory_message_after"],
                "message_upto": raw_claim["memory_message_upto"],
            },
        )

    def validate_claim(self, claim: ReviewClaim) -> bool:
        if not isinstance(claim, ReviewClaim) or claim.kind is not ReviewKind.MEMORY:
            return False
        if not isinstance(claim.session_id, str) or not claim.session_id.strip():
            return False
        if not isinstance(claim.token, str) or not claim.token:
            return False
        if not isinstance(claim.payload, Mapping):
            return False
        turn_upto = claim.payload.get("turn_upto")
        message_after = claim.payload.get("message_after")
        message_upto = claim.payload.get("message_upto")
        return (
            not isinstance(turn_upto, bool)
            and isinstance(turn_upto, int)
            and turn_upto >= 0
            and not isinstance(message_after, bool)
            and isinstance(message_after, int)
            and message_after >= 0
            and not isinstance(message_upto, bool)
            and isinstance(message_upto, int)
            and message_upto > message_after
        )

    def claim_is_valid(self, conn, claim: ReviewClaim) -> bool:
        return self.store.claim_is_valid(conn, claim.session_id, claim.token)

    def prepare_run(self, conn, claim: ReviewClaim) -> ReviewRunSpec:
        if not self.validate_claim(claim):
            raise ValueError("invalid memory review claim")
        review_messages = self.store.load_message_window(
            conn,
            claim.session_id,
            after_message_id=claim.payload["message_after"],
            upto_message_id=claim.payload["message_upto"],
        )
        return ReviewRunSpec(
            messages=review_messages,
            system_prompt=MEMORY_REVIEW_SYSTEM_PROMPT,
            instruction=MEMORY_REVIEW_INSTRUCTION,
            tool_policy=ToolPolicy(
                ExecutionEnvironment.BACKGROUND_REVIEW,
                enabled_toolsets=frozenset({"memory"}),
                unattended=True,
                allowed_approval_modes=frozenset({ApprovalMode.NONE.value}),
                max_risk_level=ToolRiskLevel.MEDIUM,
            ),
            max_iterations=self.max_iterations,
        )

    def complete(self, conn, claim: ReviewClaim) -> bool:
        return self.store.complete(conn, claim.session_id, claim.token)

    def fail(self, conn, claim: ReviewClaim, error: str) -> bool:
        return self.store.fail(
            conn,
            claim.session_id,
            claim.token,
            error=error,
            retry_cooldown_seconds=self.retry_cooldown_seconds,
        )
