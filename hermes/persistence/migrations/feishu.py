from __future__ import annotations

import sqlite3

from ..database import DBError, _derive_feishu_inbox_route_key, _table_columns, _table_exists
from ..schemas.feishu import _create_feishu_inbox_indexes_and_triggers, _create_feishu_inbox_schema, _create_feishu_pending_attachment_schema


def _migrate_v17_to_v18(conn: sqlite3.Connection) -> None:
    _create_feishu_pending_attachment_schema(conn)

def _migrate_v9_to_v10(conn: sqlite3.Connection) -> None:
    """把 Adapter 旧 Inbox 原地升级为正式 schema，并保留全部记录。"""
    if not _table_exists(conn, "feishu_message_inbox"):
        _create_feishu_inbox_schema(conn)
        return

    columns = _table_columns(conn, "feishu_message_inbox")
    required_legacy_columns = {
        "app_id",
        "message_id",
        "payload",
        "received_at",
        "status",
    }
    missing_legacy_columns = required_legacy_columns - columns
    if missing_legacy_columns:
        missing = ", ".join(sorted(missing_legacy_columns))
        raise DBError(f"Feishu Inbox missing required columns: {missing}")

    # SQLite 不能给旧表补表级 CHECK；先原地补列，后续用触发器约束状态。
    additions = (
        ("receive_sequence", "INTEGER NOT NULL DEFAULT 0"),
        ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
        ("next_attempt_at", "REAL"),
        ("last_error", "TEXT"),
        ("updated_at", "REAL NOT NULL DEFAULT 0"),
        ("completed_at", "REAL"),
        ("batch_message_id", "TEXT"),
    )
    for column_name, definition in additions:
        if column_name in columns:
            continue
        conn.execute(
            f"ALTER TABLE feishu_message_inbox "
            f"ADD COLUMN {column_name} {definition}"
        )
        columns.add(column_name)

    invalid_status = conn.execute(
        """
        SELECT status
        FROM feishu_message_inbox
        WHERE status IS NULL OR status NOT IN (
            'pending', 'processing', 'retry_wait', 'processed',
            'cancelled', 'permanent_failed'
        )
        LIMIT 1
        """
    ).fetchone()
    if invalid_status is not None:
        raise DBError(
            "cannot migrate Feishu Inbox with invalid status: "
            f"{invalid_status[0]}"
        )

    invalid_attempt_count = conn.execute(
        """
        SELECT 1
        FROM feishu_message_inbox
        WHERE attempt_count IS NULL OR attempt_count < 0
        LIMIT 1
        """
    ).fetchone()
    if invalid_attempt_count is not None:
        raise DBError("cannot migrate Feishu Inbox with invalid attempt_count")

    # 旧恢复逻辑按 received_at、message_id 排序；首次回填沿用该顺序，
    # 此后 receive_sequence 不再重算，保证重启前后顺序稳定。
    invalid_sequence = conn.execute(
        """
        SELECT 1
        FROM feishu_message_inbox
        WHERE receive_sequence IS NULL OR receive_sequence <= 0
        LIMIT 1
        """
    ).fetchone()
    duplicate_sequence = conn.execute(
        """
        SELECT 1
        FROM feishu_message_inbox
        GROUP BY app_id, receive_sequence
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()
    if invalid_sequence is not None or duplicate_sequence is not None:
        rows = conn.execute(
            """
            SELECT app_id, message_id
            FROM feishu_message_inbox
            ORDER BY app_id, received_at, message_id
            """
        ).fetchall()
        sequence_by_app: dict[str, int] = {}
        sequence_rows = []
        for app_id, message_id in rows:
            normalized_app_id = str(app_id)
            sequence = sequence_by_app.get(normalized_app_id, 0) + 1
            sequence_by_app[normalized_app_id] = sequence
            sequence_rows.append((sequence, app_id, message_id))
        conn.executemany(
            """
            UPDATE feishu_message_inbox
            SET receive_sequence=?
            WHERE app_id=? AND message_id=?
            """,
            sequence_rows,
        )

    conn.execute(
        """
        UPDATE feishu_message_inbox
        SET updated_at=COALESCE(completed_at, received_at)
        WHERE updated_at IS NULL OR updated_at <= 0
        """
    )
    _create_feishu_inbox_indexes_and_triggers(conn)


def _migrate_v10_to_v11(conn: sqlite3.Connection) -> None:
    """为 Inbox 原地补充持久 route_key，并按旧 payload 回填。"""
    if not _table_exists(conn, "feishu_message_inbox"):
        _create_feishu_inbox_schema(conn)
        return

    columns = _table_columns(conn, "feishu_message_inbox")
    if "route_key" not in columns:
        conn.execute(
            "ALTER TABLE feishu_message_inbox "
            "ADD COLUMN route_key TEXT NOT NULL DEFAULT ''"
        )

    rows = conn.execute(
        """
        SELECT app_id, message_id, payload
        FROM feishu_message_inbox
        WHERE route_key IS NULL OR route_key=''
        ORDER BY app_id, received_at, receive_sequence
        """
    ).fetchall()
    route_rows = [
        (
            _derive_feishu_inbox_route_key(
                str(app_id),
                str(message_id),
                str(payload),
            ),
            app_id,
            message_id,
        )
        for app_id, message_id, payload in rows
    ]
    if route_rows:
        conn.executemany(
            """
            UPDATE feishu_message_inbox
            SET route_key=?
            WHERE app_id=? AND message_id=?
            """,
            route_rows,
        )

    invalid_route = conn.execute(
        """
        SELECT 1
        FROM feishu_message_inbox
        WHERE route_key IS NULL OR route_key=''
        LIMIT 1
        """
    ).fetchone()
    if invalid_route is not None:
        raise DBError("cannot migrate Feishu Inbox with invalid route_key")
    _create_feishu_inbox_indexes_and_triggers(conn)

