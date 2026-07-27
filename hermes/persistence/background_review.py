"""Memory 与 Skill Review 的独立持久化接口。"""

from __future__ import annotations

import math
import sqlite3
import time
import uuid

from .core import get_last_session_message_id, get_session_messages_in_id_range
from .database import DBError, _immediate_transaction, transaction


_MEMORY_COLUMNS = (
    "session_id, turn_total, reviewed_turn_total, message_total_upto, "
    "reviewed_message_id, claim_token, claim_turn_upto, claim_message_upto, "
    "claim_started_at, retry_turn_upto, retry_message_upto, retry_after, "
    "last_attempt_at, last_success_at, last_error, updated_at"
)
_SKILL_COLUMNS = (
    "session_id, tool_batch_total, reviewed_tool_batch_total, "
    "message_total_upto, reviewed_message_id, claim_token, "
    "claim_tool_batch_upto, claim_message_upto, claim_started_at, "
    "retry_tool_batch_upto, retry_message_upto, retry_after, "
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


def _require_positive_number(
    value,
    field_name: str,
    *,
    allow_zero: bool = False,
) -> float:
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


def _require_claim_token(claim_token: str) -> str:
    if not isinstance(claim_token, str) or not claim_token:
        raise DBError("background review claim_token must be a non-empty string")
    return claim_token


def _timestamp(now: float | None) -> float:
    if now is None:
        return time.time()
    return _require_positive_number(now, "now", allow_zero=True)


def _normalize_state_times(values: dict) -> None:
    for field_name in (
        "claim_started_at",
        "retry_after",
        "last_attempt_at",
        "last_success_at",
        "updated_at",
    ):
        value = values[field_name]
        if value is None:
            if field_name == "updated_at":
                raise DBError("background review state has invalid updated_at")
            continue
        try:
            normalized = float(value)
        except (TypeError, ValueError) as exc:
            raise DBError(
                f"background review state has invalid {field_name}"
            ) from exc
        if not math.isfinite(normalized) or normalized < 0:
            raise DBError(f"background review state has invalid {field_name}")
        values[field_name] = normalized


def _require_state_integer(values: dict, field_name: str) -> int:
    value = values[field_name]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DBError(f"background review state has invalid {field_name}")
    return value


def _require_optional_state_integer(values: dict, field_name: str) -> int | None:
    value = values[field_name]
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value < 0
    ):
        raise DBError(f"background review state has invalid {field_name}")
    return value


def _validate_claim_fields(
    values: dict,
    *,
    claim_upto_field: str,
    total_field: str,
) -> None:
    token = values["claim_token"]
    claim_upto = values[claim_upto_field]
    claim_message_upto = values["claim_message_upto"]
    started_at = values["claim_started_at"]
    if token is None:
        if any(value is not None for value in (claim_upto, claim_message_upto, started_at)):
            raise DBError("background review state has inconsistent claim fields")
        return
    if not isinstance(token, str) or not token:
        raise DBError("background review state has invalid claim token")
    if claim_upto is None or claim_message_upto is None or started_at is None:
        raise DBError("background review state has inconsistent claim fields")
    if claim_upto > values[total_field] or claim_message_upto > values["message_total_upto"]:
        raise DBError("background review claim exceeds total")


def _validate_retry_fields(
    values: dict,
    *,
    retry_upto_field: str,
    reviewed_field: str,
    total_field: str,
) -> None:
    retry_upto = values[retry_upto_field]
    retry_message_upto = values["retry_message_upto"]
    if (retry_upto is None) != (retry_message_upto is None):
        raise DBError("background review state has incomplete retry window")
    if retry_upto is None:
        return
    if retry_upto < values[reviewed_field] or retry_upto > values[total_field]:
        raise DBError("background review retry exceeds waterlines")
    if (
        retry_message_upto < values["reviewed_message_id"]
        or retry_message_upto > values["message_total_upto"]
    ):
        raise DBError("background review message retry exceeds waterlines")


def _memory_state_from_row(row) -> dict | None:
    if row is None:
        return None
    values = dict(zip(_MEMORY_COLUMNS.split(", "), row))
    for field_name in (
        "turn_total",
        "reviewed_turn_total",
        "message_total_upto",
        "reviewed_message_id",
    ):
        _require_state_integer(values, field_name)
    for field_name in (
        "claim_turn_upto",
        "claim_message_upto",
        "retry_turn_upto",
        "retry_message_upto",
    ):
        _require_optional_state_integer(values, field_name)
    _normalize_state_times(values)
    if values["reviewed_turn_total"] > values["turn_total"]:
        raise DBError("background review memory waterline exceeds total")
    if values["reviewed_message_id"] > values["message_total_upto"]:
        raise DBError("background review memory message waterline exceeds total")
    _validate_claim_fields(
        values,
        claim_upto_field="claim_turn_upto",
        total_field="turn_total",
    )
    _validate_retry_fields(
        values,
        retry_upto_field="retry_turn_upto",
        reviewed_field="reviewed_turn_total",
        total_field="turn_total",
    )
    if values["claim_turn_upto"] is not None and (
        values["claim_turn_upto"] < values["reviewed_turn_total"]
        or values["claim_message_upto"] < values["reviewed_message_id"]
    ):
        raise DBError("background review memory claim is behind reviewed waterlines")
    values.update(
        pending=values["turn_total"] - values["reviewed_turn_total"],
        retry=values["retry_turn_upto"] is not None,
        inflight=values["claim_token"] is not None,
    )
    return values


def _skill_state_from_row(row) -> dict | None:
    if row is None:
        return None
    values = dict(zip(_SKILL_COLUMNS.split(", "), row))
    for field_name in (
        "tool_batch_total",
        "reviewed_tool_batch_total",
        "message_total_upto",
        "reviewed_message_id",
    ):
        _require_state_integer(values, field_name)
    for field_name in (
        "claim_tool_batch_upto",
        "claim_message_upto",
        "retry_tool_batch_upto",
        "retry_message_upto",
    ):
        _require_optional_state_integer(values, field_name)
    _normalize_state_times(values)
    if values["reviewed_tool_batch_total"] > values["tool_batch_total"]:
        raise DBError("background review skill waterline exceeds total")
    if values["reviewed_message_id"] > values["message_total_upto"]:
        raise DBError("background review skill message waterline exceeds total")
    _validate_claim_fields(
        values,
        claim_upto_field="claim_tool_batch_upto",
        total_field="tool_batch_total",
    )
    _validate_retry_fields(
        values,
        retry_upto_field="retry_tool_batch_upto",
        reviewed_field="reviewed_tool_batch_total",
        total_field="tool_batch_total",
    )
    if values["claim_tool_batch_upto"] is not None and (
        values["claim_tool_batch_upto"] < values["reviewed_tool_batch_total"]
        or values["claim_message_upto"] < values["reviewed_message_id"]
    ):
        raise DBError("background review skill claim is behind reviewed waterlines")
    values.update(
        pending=values["tool_batch_total"] - values["reviewed_tool_batch_total"],
        retry=values["retry_tool_batch_upto"] is not None,
        inflight=values["claim_token"] is not None,
    )
    return values


def get_memory_review_state(conn: sqlite3.Connection, session_id: str) -> dict | None:
    """读取 Memory Review 状态，不创建状态行。"""
    _require_session_id(session_id)
    row = conn.execute(
        f"SELECT {_MEMORY_COLUMNS} FROM memory_review_state WHERE session_id=?",
        (session_id,),
    ).fetchone()
    return _memory_state_from_row(row)


def get_skill_review_state(conn: sqlite3.Connection, session_id: str) -> dict | None:
    """读取 Skill Review 状态，不创建状态行。"""
    _require_session_id(session_id)
    row = conn.execute(
        f"SELECT {_SKILL_COLUMNS} FROM skill_review_state WHERE session_id=?",
        (session_id,),
    ).fetchone()
    return _skill_state_from_row(row)


def record_memory_review_progress(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    completed_turns: int = 0,
    message_upto: int | None = None,
    now: float | None = None,
) -> dict:
    """原子累加已完成前台任务产生的 Memory Review 进度。"""
    _require_session_id(session_id)
    completed_turns = _require_non_negative_integer(
        completed_turns,
        "completed_turns",
    )
    if completed_turns > 0:
        if (
            isinstance(message_upto, bool)
            or not isinstance(message_upto, int)
            or message_upto <= 0
        ):
            raise DBError("background review message_upto must be a positive integer")
    elif message_upto is not None:
        raise DBError("background review message_upto requires completed_turns")
    timestamp = _timestamp(now)
    with transaction(conn):
        state = get_memory_review_state(conn, session_id)
        if (
            completed_turns > 0
            and state is not None
            and message_upto < state["message_total_upto"]
        ):
            raise DBError("background review memory message boundary moved backwards")
        try:
            if state is None:
                conn.execute(
                    """
                    INSERT INTO memory_review_state (
                        session_id, turn_total, message_total_upto, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (session_id, completed_turns, message_upto or 0, timestamp),
                )
            else:
                conn.execute(
                    """
                    UPDATE memory_review_state
                    SET turn_total=turn_total + ?,
                        message_total_upto=CASE
                            WHEN ? > 0 THEN ? ELSE message_total_upto END,
                        updated_at=?
                    WHERE session_id=?
                    """,
                    (
                        completed_turns,
                        completed_turns,
                        message_upto or 0,
                        timestamp,
                        session_id,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DBError(f"memory review progress update failed: {exc}") from exc
        state = get_memory_review_state(conn, session_id)
        if state is None:
            raise DBError("memory review progress update could not be read back")
        return state


def claim_due_memory_review(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    memory_interval: int,
    claim_ttl_seconds: float,
    now: float | None = None,
) -> dict | None:
    """领取到期的固定 Memory Review 消息窗口。"""
    _require_session_id(session_id)
    memory_interval = _require_non_negative_integer(
        memory_interval,
        "memory_interval",
    )
    claim_ttl_seconds = _require_positive_number(
        claim_ttl_seconds,
        "claim_ttl_seconds",
    )
    timestamp = _timestamp(now)
    with _immediate_transaction(conn):
        try:
            conn.execute(
                """
                INSERT INTO memory_review_state (session_id, updated_at)
                VALUES (?, ?)
                ON CONFLICT(session_id) DO NOTHING
                """,
                (session_id, timestamp),
            )
        except sqlite3.IntegrityError as exc:
            raise DBError(f"memory review state creation failed: {exc}") from exc
        if memory_interval == 0:
            return None
        state = get_memory_review_state(conn, session_id)
        if state is None:
            return None
        if state["retry_after"] is not None and state["retry_after"] > timestamp:
            return None
        if state["inflight"] and state["claim_started_at"] + claim_ttl_seconds > timestamp:
            return None
        if state["inflight"]:
            turn_upto = state["claim_turn_upto"]
            message_upto = state["claim_message_upto"]
        elif state["retry"]:
            turn_upto = state["retry_turn_upto"]
            message_upto = state["retry_message_upto"]
        elif state["pending"] >= memory_interval:
            turn_upto = state["turn_total"]
            message_upto = state["message_total_upto"]
        else:
            return None
        if message_upto <= state["reviewed_message_id"]:
            raise DBError("memory review claim has no message window")
        claim_token = str(uuid.uuid4())
        changed = conn.execute(
            """
            UPDATE memory_review_state
            SET claim_token=?, claim_turn_upto=?, claim_message_upto=?,
                claim_started_at=?, retry_after=NULL, last_attempt_at=?,
                last_error=NULL, updated_at=?
            WHERE session_id=?
            """,
            (
                claim_token,
                turn_upto,
                message_upto,
                timestamp,
                timestamp,
                timestamp,
                session_id,
            ),
        ).rowcount
        if changed != 1:
            raise DBError("memory review claim could not be recorded")
    return {
        "session_id": session_id,
        "claim_token": claim_token,
        "turn_upto": turn_upto,
        "message_after": state["reviewed_message_id"],
        "message_upto": message_upto,
    }


def memory_review_claim_is_valid(
    conn: sqlite3.Connection,
    session_id: str,
    claim_token: str,
) -> bool:
    """只读确认 Memory Review 的领取凭证仍然有效。"""
    _require_session_id(session_id)
    _require_claim_token(claim_token)
    row = conn.execute(
        """
        SELECT 1 FROM memory_review_state
        WHERE session_id=? AND claim_token=?
        """,
        (session_id, claim_token),
    ).fetchone()
    return row is not None


def complete_memory_review_claim(
    conn: sqlite3.Connection,
    session_id: str,
    claim_token: str,
    *,
    now: float | None = None,
) -> bool:
    """完成匹配 token 的 Memory Review，并推进其固定水位。"""
    _require_session_id(session_id)
    _require_claim_token(claim_token)
    timestamp = _timestamp(now)
    with _immediate_transaction(conn):
        changed = conn.execute(
            """
            UPDATE memory_review_state
            SET reviewed_turn_total=MAX(
                    reviewed_turn_total,
                    COALESCE(claim_turn_upto, reviewed_turn_total)
                ),
                reviewed_message_id=MAX(
                    reviewed_message_id,
                    COALESCE(claim_message_upto, reviewed_message_id)
                ),
                claim_token=NULL, claim_turn_upto=NULL,
                claim_message_upto=NULL, claim_started_at=NULL,
                retry_turn_upto=NULL, retry_message_upto=NULL,
                retry_after=NULL, last_success_at=?, last_error=NULL,
                updated_at=?
            WHERE session_id=? AND claim_token=?
            """,
            (timestamp, timestamp, session_id, claim_token),
        ).rowcount
        return changed == 1


def fail_memory_review_claim(
    conn: sqlite3.Connection,
    session_id: str,
    claim_token: str,
    *,
    error: str,
    retry_cooldown_seconds: float,
    now: float | None = None,
) -> bool:
    """释放失败的 Memory Review，并保留同一窗口用于重试。"""
    _require_session_id(session_id)
    _require_claim_token(claim_token)
    if not isinstance(error, str):
        raise DBError("background review error must be a string")
    cooldown = _require_positive_number(
        retry_cooldown_seconds,
        "retry_cooldown_seconds",
        allow_zero=True,
    )
    timestamp = _timestamp(now)
    with _immediate_transaction(conn):
        changed = conn.execute(
            """
            UPDATE memory_review_state
            SET retry_turn_upto=claim_turn_upto,
                retry_message_upto=claim_message_upto,
                claim_token=NULL, claim_turn_upto=NULL,
                claim_message_upto=NULL, claim_started_at=NULL,
                retry_after=?, last_error=?, updated_at=?
            WHERE session_id=? AND claim_token=?
            """,
            (timestamp + cooldown, error[:4000], timestamp, session_id, claim_token),
        ).rowcount
        return changed == 1


def load_memory_review_messages(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    after_message_id: int,
    upto_message_id: int,
) -> list[dict]:
    """读取 Memory Review 已领取的固定消息窗口。"""
    _require_session_id(session_id)
    return get_session_messages_in_id_range(
        conn,
        session_id,
        after_message_id=after_message_id,
        upto_message_id=upto_message_id,
    )


def get_last_memory_review_message_id(
    conn: sqlite3.Connection,
    session_id: str,
) -> int | None:
    """返回 Memory Review 记录新进度所需的最后一条消息标识。"""
    _require_session_id(session_id)
    return get_last_session_message_id(conn, session_id)


def record_skill_review_progress(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    tool_batches: int = 0,
    message_upto: int | None = None,
    now: float | None = None,
) -> dict:
    """原子累加已完成前台任务产生的 Skill Review 进度。"""
    _require_session_id(session_id)
    tool_batches = _require_non_negative_integer(tool_batches, "tool_batches")
    if tool_batches > 0:
        if (
            isinstance(message_upto, bool)
            or not isinstance(message_upto, int)
            or message_upto <= 0
        ):
            raise DBError("background review message_upto must be a positive integer")
    elif message_upto is not None and (
        isinstance(message_upto, bool)
        or not isinstance(message_upto, int)
        or message_upto <= 0
    ):
        raise DBError("background review message_upto must be a positive integer")
    timestamp = _timestamp(now)
    with transaction(conn):
        state = get_skill_review_state(conn, session_id)
        if (
            message_upto is not None
            and state is not None
            and message_upto < state["message_total_upto"]
        ):
            raise DBError("background review skill message boundary moved backwards")
        try:
            if state is None:
                conn.execute(
                    """
                    INSERT INTO skill_review_state (
                        session_id, tool_batch_total, message_total_upto, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (session_id, tool_batches, message_upto or 0, timestamp),
                )
            else:
                conn.execute(
                    """
                    UPDATE skill_review_state
                    SET tool_batch_total=tool_batch_total + ?,
                        message_total_upto=CASE
                            WHEN ? IS NOT NULL THEN ? ELSE message_total_upto END,
                        updated_at=?
                    WHERE session_id=?
                    """,
                    (
                        tool_batches,
                        message_upto,
                        message_upto,
                        timestamp,
                        session_id,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DBError(f"skill review progress update failed: {exc}") from exc
        state = get_skill_review_state(conn, session_id)
        if state is None:
            raise DBError("skill review progress update could not be read back")
        return state


def claim_due_skill_review(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    skill_tool_batch_interval: int,
    claim_ttl_seconds: float,
    now: float | None = None,
) -> dict | None:
    """领取到期的固定 Skill Review 消息窗口。"""
    _require_session_id(session_id)
    skill_tool_batch_interval = _require_non_negative_integer(
        skill_tool_batch_interval,
        "skill_tool_batch_interval",
    )
    claim_ttl_seconds = _require_positive_number(
        claim_ttl_seconds,
        "claim_ttl_seconds",
    )
    timestamp = _timestamp(now)
    with _immediate_transaction(conn):
        try:
            conn.execute(
                """
                INSERT INTO skill_review_state (session_id, updated_at)
                VALUES (?, ?)
                ON CONFLICT(session_id) DO NOTHING
                """,
                (session_id, timestamp),
            )
        except sqlite3.IntegrityError as exc:
            raise DBError(f"skill review state creation failed: {exc}") from exc
        if skill_tool_batch_interval == 0:
            return None
        state = get_skill_review_state(conn, session_id)
        if state is None:
            return None
        if state["retry_after"] is not None and state["retry_after"] > timestamp:
            return None
        if state["inflight"] and state["claim_started_at"] + claim_ttl_seconds > timestamp:
            return None
        if state["inflight"]:
            tool_batch_upto = state["claim_tool_batch_upto"]
            message_upto = state["claim_message_upto"]
        elif state["retry"]:
            tool_batch_upto = state["retry_tool_batch_upto"]
            message_upto = state["retry_message_upto"]
        elif state["pending"] >= skill_tool_batch_interval:
            tool_batch_upto = state["tool_batch_total"]
            message_upto = state["message_total_upto"]
        else:
            return None
        if message_upto <= state["reviewed_message_id"]:
            raise DBError("skill review claim has no message window")
        claim_token = str(uuid.uuid4())
        changed = conn.execute(
            """
            UPDATE skill_review_state
            SET claim_token=?, claim_tool_batch_upto=?, claim_message_upto=?,
                claim_started_at=?, retry_after=NULL, last_attempt_at=?,
                last_error=NULL, updated_at=?
            WHERE session_id=?
            """,
            (
                claim_token,
                tool_batch_upto,
                message_upto,
                timestamp,
                timestamp,
                timestamp,
                session_id,
            ),
        ).rowcount
        if changed != 1:
            raise DBError("skill review claim could not be recorded")
    return {
        "session_id": session_id,
        "claim_token": claim_token,
        "tool_batch_upto": tool_batch_upto,
        "message_after": state["reviewed_message_id"],
        "message_upto": message_upto,
    }


def skill_review_claim_is_valid(
    conn: sqlite3.Connection,
    session_id: str,
    claim_token: str,
) -> bool:
    """只读确认 Skill Review 的领取凭证仍然有效。"""
    _require_session_id(session_id)
    _require_claim_token(claim_token)
    row = conn.execute(
        """
        SELECT 1 FROM skill_review_state
        WHERE session_id=? AND claim_token=?
        """,
        (session_id, claim_token),
    ).fetchone()
    return row is not None


def complete_skill_review_claim(
    conn: sqlite3.Connection,
    session_id: str,
    claim_token: str,
    *,
    now: float | None = None,
) -> bool:
    """完成匹配 token 的 Skill Review，并推进其固定水位。"""
    _require_session_id(session_id)
    _require_claim_token(claim_token)
    timestamp = _timestamp(now)
    with _immediate_transaction(conn):
        changed = conn.execute(
            """
            UPDATE skill_review_state
            SET reviewed_tool_batch_total=MAX(
                    reviewed_tool_batch_total,
                    COALESCE(
                        claim_tool_batch_upto,
                        reviewed_tool_batch_total
                    )
                ),
                reviewed_message_id=MAX(
                    reviewed_message_id,
                    COALESCE(claim_message_upto, reviewed_message_id)
                ),
                claim_token=NULL, claim_tool_batch_upto=NULL,
                claim_message_upto=NULL, claim_started_at=NULL,
                retry_tool_batch_upto=NULL, retry_message_upto=NULL,
                retry_after=NULL, last_success_at=?, last_error=NULL,
                updated_at=?
            WHERE session_id=? AND claim_token=?
            """,
            (timestamp, timestamp, session_id, claim_token),
        ).rowcount
        return changed == 1


def fail_skill_review_claim(
    conn: sqlite3.Connection,
    session_id: str,
    claim_token: str,
    *,
    error: str,
    retry_cooldown_seconds: float,
    now: float | None = None,
) -> bool:
    """释放失败的 Skill Review，并保留同一窗口用于重试。"""
    _require_session_id(session_id)
    _require_claim_token(claim_token)
    if not isinstance(error, str):
        raise DBError("background review error must be a string")
    cooldown = _require_positive_number(
        retry_cooldown_seconds,
        "retry_cooldown_seconds",
        allow_zero=True,
    )
    timestamp = _timestamp(now)
    with _immediate_transaction(conn):
        changed = conn.execute(
            """
            UPDATE skill_review_state
            SET retry_tool_batch_upto=claim_tool_batch_upto,
                retry_message_upto=claim_message_upto,
                claim_token=NULL, claim_tool_batch_upto=NULL,
                claim_message_upto=NULL, claim_started_at=NULL,
                retry_after=?, last_error=?, updated_at=?
            WHERE session_id=? AND claim_token=?
            """,
            (timestamp + cooldown, error[:4000], timestamp, session_id, claim_token),
        ).rowcount
        return changed == 1


def load_skill_review_messages(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    after_message_id: int,
    upto_message_id: int,
) -> list[dict]:
    """读取 Skill Review 已领取的固定消息窗口。"""
    _require_session_id(session_id)
    return get_session_messages_in_id_range(
        conn,
        session_id,
        after_message_id=after_message_id,
        upto_message_id=upto_message_id,
    )


def get_last_skill_review_message_id(
    conn: sqlite3.Connection,
    session_id: str,
) -> int | None:
    """返回 Skill Review 记录新进度所需的最后一条消息标识。"""
    _require_session_id(session_id)
    return get_last_session_message_id(conn, session_id)
