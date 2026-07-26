"""Background Review 的会话级持久化状态与原子领取接口。"""

from __future__ import annotations

import math
import sqlite3
import time
import uuid

from .database import DBError, _immediate_transaction, transaction


_STATE_COLUMNS = (
    "session_id, memory_turn_total, memory_reviewed_total, "
    "skill_tool_batch_total, skill_reviewed_total, claim_token, "
    "claim_memory_upto, claim_skill_upto, claim_started_at, retry_after, "
    "last_attempt_at, last_success_at, last_error, updated_at"
)


def _require_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not session_id.strip():
        raise DBError("background review session_id must be a non-empty string")
    return session_id


def _require_non_negative_integer(value, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DBError(f"background review {field_name} must be a non-negative integer")
    return value


def _require_positive_number(value, field_name: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool):
        raise DBError(f"background review {field_name} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise DBError(f"background review {field_name} must be a number") from exc
    if not math.isfinite(result) or result < 0 or (not allow_zero and result == 0):
        qualifier = "non-negative" if allow_zero else "positive"
        raise DBError(f"background review {field_name} must be {qualifier}")
    return result


def _timestamp(now: float | None) -> float:
    if now is None:
        return time.time()
    return _require_positive_number(now, "now", allow_zero=True)


def _state_from_row(row) -> dict | None:
    if row is None:
        return None
    values = dict(zip(_STATE_COLUMNS.split(", "), row))
    integer_fields = (
        "memory_turn_total", "memory_reviewed_total",
        "skill_tool_batch_total", "skill_reviewed_total",
    )
    for field_name in integer_fields:
        value = values[field_name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DBError(f"background review state has invalid {field_name}")
    for field_name in ("claim_memory_upto", "claim_skill_upto"):
        value = values[field_name]
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise DBError(f"background review state has invalid {field_name}")
    if values["memory_reviewed_total"] > values["memory_turn_total"]:
        raise DBError("background review memory waterline exceeds total")
    if values["skill_reviewed_total"] > values["skill_tool_batch_total"]:
        raise DBError("background review skill waterline exceeds total")
    if (
        values["claim_memory_upto"] is not None
        and values["claim_memory_upto"] > values["memory_turn_total"]
    ):
        raise DBError("background review memory claim exceeds total")
    if (
        values["claim_skill_upto"] is not None
        and values["claim_skill_upto"] > values["skill_tool_batch_total"]
    ):
        raise DBError("background review skill claim exceeds total")
    for field_name in (
        "claim_started_at", "retry_after", "last_attempt_at",
        "last_success_at", "updated_at",
    ):
        value = values[field_name]
        if value is None:
            if field_name == "updated_at":
                raise DBError("background review state has invalid updated_at")
            continue
        try:
            normalized = float(value)
        except (TypeError, ValueError) as exc:
            raise DBError(f"background review state has invalid {field_name}") from exc
        if not math.isfinite(normalized) or normalized < 0:
            raise DBError(f"background review state has invalid {field_name}")
        values[field_name] = normalized

    claim_token = values["claim_token"]
    if claim_token is None and any(
        values[field_name] is not None
        for field_name in ("claim_memory_upto", "claim_skill_upto", "claim_started_at")
    ):
        raise DBError("background review state has inconsistent claim fields")
    if claim_token is not None:
        if not isinstance(claim_token, str) or not claim_token:
            raise DBError("background review state has invalid claim token")
        if values["claim_started_at"] is None or (
            values["claim_memory_upto"] is None
            and values["claim_skill_upto"] is None
        ):
            raise DBError("background review state has inconsistent claim fields")

    memory_pending = values["memory_turn_total"] - values["memory_reviewed_total"]
    skill_pending = values["skill_tool_batch_total"] - values["skill_reviewed_total"]
    if memory_pending < 0 or skill_pending < 0:
        raise DBError("background review state has negative pending progress")
    values.update(
        memory_pending=memory_pending,
        skill_pending=skill_pending,
        review_memory=values["claim_memory_upto"] is not None,
        review_skills=values["claim_skill_upto"] is not None,
        inflight=claim_token is not None,
    )
    return values


def get_background_review_state(
    conn: sqlite3.Connection,
    session_id: str,
) -> dict | None:
    """读取会话审视状态；查询本身不创建状态行。"""
    _require_session_id(session_id)
    row = conn.execute(
        f"SELECT {_STATE_COLUMNS} FROM background_review_state WHERE session_id=?",
        (session_id,),
    ).fetchone()
    return _state_from_row(row)


def background_review_claim_is_valid(
    conn: sqlite3.Connection,
    session_id: str,
    claim_token: str,
) -> bool:
    """只读确认指定领取凭证是否仍属于当前会话状态。"""
    _require_session_id(session_id)
    if not isinstance(claim_token, str) or not claim_token:
        raise DBError("background review claim_token must be a non-empty string")
    row = conn.execute(
        """
        SELECT 1 FROM background_review_state
        WHERE session_id=? AND claim_token=?
        """,
        (session_id, claim_token),
    ).fetchone()
    return row is not None


def record_background_review_progress(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    memory_turns: int = 0,
    skill_tool_batches: int = 0,
    now: float | None = None,
) -> dict:
    """原子累加会话新产生的轮次与工具批次。"""
    _require_session_id(session_id)
    memory_turns = _require_non_negative_integer(memory_turns, "memory_turns")
    skill_tool_batches = _require_non_negative_integer(
        skill_tool_batches, "skill_tool_batches"
    )
    timestamp = _timestamp(now)
    with transaction(conn):
        try:
            conn.execute(
                """
                INSERT INTO background_review_state (
                    session_id, memory_turn_total, skill_tool_batch_total, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    memory_turn_total=background_review_state.memory_turn_total
                        + excluded.memory_turn_total,
                    skill_tool_batch_total=background_review_state.skill_tool_batch_total
                        + excluded.skill_tool_batch_total,
                    updated_at=excluded.updated_at
                """,
                (session_id, memory_turns, skill_tool_batches, timestamp),
            )
        except sqlite3.IntegrityError as exc:
            raise DBError(f"background review progress update failed: {exc}") from exc
        state = get_background_review_state(conn, session_id)
        if state is None:
            raise DBError("background review progress update could not be read back")
        return state


def mark_background_review_handled(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    memory: bool = False,
    skills: bool = False,
    now: float | None = None,
) -> dict:
    """把前台已显式处理的进度水位推进到当前累计总量。"""
    _require_session_id(session_id)
    if not isinstance(memory, bool) or not isinstance(skills, bool):
        raise DBError("background review handled flags must be boolean")
    if not memory and not skills:
        raise DBError("background review must handle memory or skills")
    timestamp = _timestamp(now)
    with transaction(conn):
        changed = conn.execute(
            """
            UPDATE background_review_state
            SET memory_reviewed_total=CASE
                    WHEN ? THEN memory_turn_total ELSE memory_reviewed_total END,
                skill_reviewed_total=CASE
                    WHEN ? THEN skill_tool_batch_total ELSE skill_reviewed_total END,
                updated_at=?
            WHERE session_id=?
            """,
            (int(memory), int(skills), timestamp, session_id),
        ).rowcount
        if changed != 1:
            raise DBError("background review state not found")
        state = get_background_review_state(conn, session_id)
        if state is None:
            raise DBError("background review handled state could not be read back")
        return state


def claim_due_background_review(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    memory_interval: int,
    skill_interval: int,
    claim_ttl_seconds: float,
    now: float | None = None,
) -> dict | None:
    """在 SQLite 写锁内领取一个到期的会话审视任务。"""
    _require_session_id(session_id)
    memory_interval = _require_non_negative_integer(
        memory_interval, "memory_interval"
    )
    skill_interval = _require_non_negative_integer(skill_interval, "skill_interval")
    claim_ttl_seconds = _require_positive_number(
        claim_ttl_seconds, "claim_ttl_seconds"
    )
    timestamp = _timestamp(now)
    with _immediate_transaction(conn):
        try:
            conn.execute(
                """
                INSERT INTO background_review_state (session_id, updated_at)
                VALUES (?, ?)
                ON CONFLICT(session_id) DO NOTHING
                """,
                (session_id, timestamp),
            )
        except sqlite3.IntegrityError as exc:
            raise DBError(f"background review state creation failed: {exc}") from exc
        if memory_interval == 0 and skill_interval == 0:
            return None
        state = get_background_review_state(conn, session_id)
        if state is None:
            return None
        if state["retry_after"] is not None and state["retry_after"] > timestamp:
            return None
        if state["inflight"] and (
            state["claim_started_at"] + claim_ttl_seconds > timestamp
        ):
            return None

        review_memory = (
            memory_interval > 0 and state["memory_pending"] >= memory_interval
        )
        review_skills = (
            skill_interval > 0 and state["skill_pending"] >= skill_interval
        )
        if not review_memory and not review_skills:
            return None

        claim_token = str(uuid.uuid4())
        memory_upto = state["memory_turn_total"] if review_memory else None
        skill_upto = state["skill_tool_batch_total"] if review_skills else None
        changed = conn.execute(
            """
            UPDATE background_review_state
            SET claim_token=?, claim_memory_upto=?, claim_skill_upto=?,
                claim_started_at=?, retry_after=NULL, last_attempt_at=?,
                last_error=NULL, updated_at=?
            WHERE session_id=?
            """,
            (
                claim_token, memory_upto, skill_upto, timestamp, timestamp,
                timestamp, session_id,
            ),
        ).rowcount
        if changed != 1:
            raise DBError("background review claim could not be recorded")
    return {
        "session_id": session_id,
        "claim_token": claim_token,
        "review_memory": review_memory,
        "review_skills": review_skills,
        "memory_upto": memory_upto,
        "skill_upto": skill_upto,
    }


def complete_background_review_claim(
    conn: sqlite3.Connection,
    session_id: str,
    claim_token: str,
    *,
    now: float | None = None,
) -> bool:
    """完成与 token 匹配的领取，并仅推进其领取时的处理上限。"""
    _require_session_id(session_id)
    if not isinstance(claim_token, str) or not claim_token:
        raise DBError("background review claim_token must be a non-empty string")
    timestamp = _timestamp(now)
    with _immediate_transaction(conn):
        changed = conn.execute(
            """
            UPDATE background_review_state
            SET memory_reviewed_total=MAX(
                    memory_reviewed_total,
                    COALESCE(claim_memory_upto, memory_reviewed_total)
                ),
                skill_reviewed_total=MAX(
                    skill_reviewed_total,
                    COALESCE(claim_skill_upto, skill_reviewed_total)
                ),
                claim_token=NULL, claim_memory_upto=NULL, claim_skill_upto=NULL,
                claim_started_at=NULL, retry_after=NULL, last_success_at=?,
                last_error=NULL, updated_at=?
            WHERE session_id=? AND claim_token=?
            """,
            (timestamp, timestamp, session_id, claim_token),
        ).rowcount
        return changed == 1


def fail_background_review_claim(
    conn: sqlite3.Connection,
    session_id: str,
    claim_token: str,
    *,
    error: str,
    retry_cooldown_seconds: float,
    now: float | None = None,
) -> bool:
    """释放与 token 匹配的失败领取，并保留待处理进度供冷却后重试。"""
    _require_session_id(session_id)
    if not isinstance(claim_token, str) or not claim_token:
        raise DBError("background review claim_token must be a non-empty string")
    if not isinstance(error, str):
        raise DBError("background review error must be a string")
    cooldown = _require_positive_number(
        retry_cooldown_seconds, "retry_cooldown_seconds", allow_zero=True
    )
    timestamp = _timestamp(now)
    with _immediate_transaction(conn):
        changed = conn.execute(
            """
            UPDATE background_review_state
            SET claim_token=NULL, claim_memory_upto=NULL, claim_skill_upto=NULL,
                claim_started_at=NULL, retry_after=?, last_error=?, updated_at=?
            WHERE session_id=? AND claim_token=?
            """,
            (
                timestamp + cooldown, error[:4000], timestamp, session_id,
                claim_token,
            ),
        ).rowcount
        return changed == 1
