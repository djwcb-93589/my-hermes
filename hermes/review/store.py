"""Memory Review 与既有持久化接口之间的适配层。"""

from __future__ import annotations

import sqlite3

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


class MemoryReviewStore:
    """只调用既有持久化公共接口的 Memory Review 存储适配器。"""

    def record_progress(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        *,
        completed_turns: int,
        message_upto: int | None,
    ) -> None:
        record_background_review_progress(
            conn,
            session_id,
            memory_turns=completed_turns,
            memory_message_upto=message_upto,
            skill_tool_batches=0,
        )

    def claim_due(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        *,
        memory_interval: int,
        claim_ttl_seconds: float,
    ) -> dict | None:
        return claim_due_background_review(
            conn,
            session_id,
            memory_interval=memory_interval,
            skill_interval=0,
            claim_ttl_seconds=claim_ttl_seconds,
        )

    def load_message_window(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        *,
        after_message_id: int,
        upto_message_id: int,
    ) -> list[dict]:
        return get_session_messages_in_id_range(
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
        return get_last_session_message_id(conn, session_id)

    def claim_is_valid(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        token: str,
    ) -> bool:
        return background_review_claim_is_valid(conn, session_id, token)

    def complete(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        token: str,
    ) -> bool:
        return complete_background_review_claim(conn, session_id, token)

    def fail(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        token: str,
        *,
        error: str,
        retry_cooldown_seconds: float,
    ) -> bool:
        return fail_background_review_claim(
            conn,
            session_id,
            token,
            error=error,
            retry_cooldown_seconds=retry_cooldown_seconds,
        )
