"""Dashboard 对持久化数据库和 Gateway lease 的只读健康检查。"""

from __future__ import annotations

import math
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from hermes.gateway.constants import GATEWAY_RUNTIME_LEASE_NAME
from hermes.persistence.read_only import readonly_connection
from hermes.persistence.schema import LATEST_SCHEMA_VERSION
from hermes.web.schemas import DatabaseHealth, GatewayHealth


_REQUIRED_TABLES = frozenset({
    "schema_version",
    "sessions",
    "messages",
    "cron_jobs",
    "cron_runs",
    "gateway_runtime_lease",
    "tool_executions",
    "model_call_events",
    "observations",
    "runtime_component_snapshots",
})


def inspect_database_health(db_path: str | None) -> DatabaseHealth:
    """只读验证数据库版本和关键表，不建库、不迁移也不返回底层异常。"""
    expected = LATEST_SCHEMA_VERSION
    path_state = _database_path_state(db_path)
    if path_state == "missing":
        return DatabaseHealth(
            status="unavailable",
            schema_expected=expected,
            schema_actual=None,
            required_tables_available=False,
            reason_code="db_path_missing",
        )
    if path_state != "file":
        return _unavailable_database_health(expected, "db_open_failed")

    try:
        with readonly_connection(str(db_path)) as conn:
            try:
                tables = _table_names(conn)
            except sqlite3.Error:
                return _unavailable_database_health(expected, "query_failed")

            required_tables_available = _REQUIRED_TABLES <= tables
            if "schema_version" not in tables:
                return DatabaseHealth(
                    status="degraded",
                    schema_expected=expected,
                    schema_actual=None,
                    required_tables_available=False,
                    reason_code="schema_table_missing",
                )

            try:
                row = conn.execute(
                    "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
                ).fetchone()
            except sqlite3.Error:
                return _unavailable_database_health(expected, "query_failed")
    except (sqlite3.Error, OSError, ValueError):
        return _unavailable_database_health(expected, "db_open_failed")

    actual = _schema_version(row)
    if actual is None:
        return DatabaseHealth(
            status="degraded",
            schema_expected=expected,
            schema_actual=None,
            required_tables_available=required_tables_available,
            reason_code="schema_version_missing",
        )
    if not required_tables_available:
        return DatabaseHealth(
            status="degraded",
            schema_expected=expected,
            schema_actual=actual,
            required_tables_available=False,
            reason_code="required_table_missing",
        )
    if actual != expected:
        return DatabaseHealth(
            status="degraded",
            schema_expected=expected,
            schema_actual=actual,
            required_tables_available=True,
            reason_code="schema_version_mismatch",
        )
    return DatabaseHealth(
        status="healthy",
        schema_expected=expected,
        schema_actual=actual,
        required_tables_available=True,
        reason_code=None,
    )


def inspect_gateway_health(db_path: str | None) -> GatewayHealth:
    """只读检查 Gateway runtime lease，不竞争、续租或释放 lease。"""
    path_state = _database_path_state(db_path)
    if path_state == "missing":
        return GatewayHealth(status="unavailable", reason_code="db_path_missing")
    if path_state != "file":
        return GatewayHealth(status="unavailable", reason_code="db_open_failed")

    try:
        with readonly_connection(str(db_path)) as conn:
            try:
                tables = _table_names(conn)
                if "gateway_runtime_lease" not in tables:
                    return GatewayHealth(
                        status="unavailable",
                        reason_code="lease_table_missing",
                    )
                row = conn.execute(
                    "SELECT heartbeat_at, expires_at "
                    "FROM gateway_runtime_lease WHERE lease_name = ?",
                    (GATEWAY_RUNTIME_LEASE_NAME,),
                ).fetchone()
            except sqlite3.Error:
                return GatewayHealth(status="unavailable", reason_code="lease_query_failed")
    except (sqlite3.Error, OSError, ValueError):
        return GatewayHealth(status="unavailable", reason_code="db_open_failed")

    if row is None:
        return GatewayHealth(status="stopped", reason_code="lease_not_found")

    now = time.time()
    heartbeat_at, expires_at = _lease_snapshot(row)
    if _lease_is_valid((heartbeat_at, expires_at), now):
        return GatewayHealth(
            status="running",
            reason_code=None,
            heartbeat_at=_timestamp_to_utc(heartbeat_at),
            expires_at=_timestamp_to_utc(expires_at),
        )

    if expires_at is not None and expires_at <= 0:
        return GatewayHealth(
            status="stopped",
            reason_code="lease_released",
            heartbeat_at=_timestamp_to_utc(heartbeat_at),
            expires_at=_timestamp_to_utc(expires_at),
        )
    if heartbeat_at is None or expires_at is None:
        reason_code = "lease_timestamp_invalid"
    elif heartbeat_at > expires_at:
        reason_code = "lease_heartbeat_invalid"
    else:
        reason_code = "lease_expired"
    return GatewayHealth(
        status="stale",
        reason_code=reason_code,
        heartbeat_at=_timestamp_to_utc(heartbeat_at),
        expires_at=_timestamp_to_utc(expires_at),
    )


def _database_path_state(db_path: str | None) -> str:
    """先区分路径缺失和只读打开失败，且不触发 SQLite 文件创建。"""
    if not isinstance(db_path, str) or not db_path.strip():
        return "missing"
    try:
        path = Path(db_path)
        if not path.exists():
            return "missing"
        return "file" if path.is_file() else "invalid"
    except OSError:
        return "invalid"


def _table_names(conn: sqlite3.Connection) -> set[str]:
    """读取当前数据库已有表名，不执行任何 DDL。"""
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _schema_version(row: object) -> int | None:
    """将 schema_version 行安全转换为整数版本。"""
    if not isinstance(row, tuple) or len(row) != 1:
        return None
    value = row[0]
    if isinstance(value, bool):
        return None
    try:
        version = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return version if version >= 0 else None


def _unavailable_database_health(expected: int, reason_code: str) -> DatabaseHealth:
    """构造不携带连接错误文本的数据库不可用响应。"""
    return DatabaseHealth(
        status="unavailable",
        schema_expected=expected,
        schema_actual=None,
        required_tables_available=False,
        reason_code=reason_code,
    )


def _lease_snapshot(row: object) -> tuple[float | None, float | None]:
    """校验 lease 时间戳，异常值只影响状态判断而不泄漏底层数据。"""
    if not isinstance(row, tuple) or len(row) != 2:
        return None, None
    return _finite_timestamp(row[0]), _finite_timestamp(row[1])


def _finite_timestamp(value: object) -> float | None:
    """返回有限的 Unix 时间戳，拒绝布尔值和非数值。"""
    if isinstance(value, bool):
        return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return timestamp if math.isfinite(timestamp) else None


def _lease_is_valid(snapshot: tuple[float | None, float | None], now: float) -> bool:
    """有效 lease 必须尚未过期，且心跳时间不能晚于其到期时间。"""
    heartbeat_at, expires_at = snapshot
    return (
        heartbeat_at is not None
        and expires_at is not None
        and heartbeat_at <= expires_at
        and expires_at > now
    )


def _timestamp_to_utc(value: float | None) -> datetime | None:
    """将公开的 Unix 时间戳转为带 UTC 时区的时间。"""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return None
