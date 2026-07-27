"""Memory Review 与其独立持久化接口之间的适配层。"""

from __future__ import annotations

import sqlite3

from hermes.persistence.background_review import (
    claim_due_memory_review,
    complete_memory_review_claim,
    fail_memory_review_claim,
    get_last_memory_review_message_id,
    load_memory_review_messages,
    memory_review_claim_is_valid,
    record_memory_review_progress,
)


class MemoryReviewStore:
    """只封装 Memory Review 的持久化操作。"""

    def record_progress(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        *,
        completed_turns: int,
        message_upto: int | None,
    ) -> None:
        record_memory_review_progress(
            conn,
            session_id,
            completed_turns=completed_turns,
            message_upto=message_upto,
        )

    def claim_due(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        *,
        memory_interval: int,
        claim_ttl_seconds: float,
    ) -> dict | None:
        claim = claim_due_memory_review(
            conn,
            session_id,
            memory_interval=memory_interval,
            claim_ttl_seconds=claim_ttl_seconds,
        )
        if claim is None:
            return None
        return {
            "session_id": claim["session_id"],
            "claim_token": claim["claim_token"],
            "turn_upto": claim["turn_upto"],
            "message_after": claim["message_after"],
            "message_upto": claim["message_upto"],
        }

    def load_message_window(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        *,
        after_message_id: int,
        upto_message_id: int,
    ) -> list[dict]:
        return load_memory_review_messages(
            conn,
            session_id,
            after_message_id=after_message_id,
            upto_message_id=upto_message_id,
        )

    def get_last_message_id(
        self,
        conn: sqlite3.Connection,
        session_id: str,
    ) -> int | None:
        return get_last_memory_review_message_id(conn, session_id)

    def claim_is_valid(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        token: str,
    ) -> bool:
        return memory_review_claim_is_valid(conn, session_id, token)

    def complete(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        token: str,
    ) -> bool:
        return complete_memory_review_claim(conn, session_id, token)

    def fail(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        token: str,
        *,
        error: str,
        retry_cooldown_seconds: float,
    ) -> bool:
        return fail_memory_review_claim(
            conn,
            session_id,
            token,
            error=error,
            retry_cooldown_seconds=retry_cooldown_seconds,
        )
