"""Skill Review 与其独立持久化接口之间的适配层。"""

from __future__ import annotations

import sqlite3

from hermes.persistence.background_review import (
    claim_due_skill_review,
    complete_skill_review_claim,
    fail_skill_review_claim,
    get_last_skill_review_message_id,
    load_skill_review_messages,
    record_skill_review_progress,
    skill_review_claim_is_valid,
)


class SkillReviewStore:
    """只封装 Skill Review 的持久化操作，不负责启动审视任务。"""

    def record_progress(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        *,
        tool_batches: int,
        message_upto: int | None,
    ) -> None:
        record_skill_review_progress(
            conn,
            session_id,
            tool_batches=tool_batches,
            message_upto=message_upto,
        )

    def claim_due(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        *,
        skill_interval: int,
        claim_ttl_seconds: float,
    ) -> dict | None:
        return claim_due_skill_review(
            conn,
            session_id,
            skill_interval=skill_interval,
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
        return load_skill_review_messages(
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
        return get_last_skill_review_message_id(conn, session_id)

    def claim_is_valid(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        token: str,
    ) -> bool:
        return skill_review_claim_is_valid(conn, session_id, token)

    def complete(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        token: str,
    ) -> bool:
        return complete_skill_review_claim(conn, session_id, token)

    def fail(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        token: str,
        *,
        error: str,
        retry_cooldown_seconds: float,
    ) -> bool:
        return fail_skill_review_claim(
            conn,
            session_id,
            token,
            error=error,
            retry_cooldown_seconds=retry_cooldown_seconds,
        )
