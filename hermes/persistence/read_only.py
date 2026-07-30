"""为管理查询提供不建库、不迁移的 SQLite 只读连接。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_DEFAULT_BUSY_TIMEOUT_MS = 5_000
_MAX_BUSY_TIMEOUT_MS = 60_000


def _readonly_uri(db_path: str | Path) -> str:
    """把已有数据库路径转换为 SQLite 只读 URI，不触碰文件系统。"""
    return f"{Path(db_path).absolute().as_uri()}?mode=ro"


@contextmanager
def readonly_connection(
    db_path: str | Path,
    *,
    busy_timeout_ms: int = _DEFAULT_BUSY_TIMEOUT_MS,
) -> Iterator[sqlite3.Connection]:
    """在调用范围内打开已有数据库，并在退出时关闭连接。"""
    if (
        type(busy_timeout_ms) is not int
        or busy_timeout_ms < 0
        or busy_timeout_ms > _MAX_BUSY_TIMEOUT_MS
    ):
        raise ValueError("busy_timeout_ms is outside the read-only boundary")
    connection = sqlite3.connect(
        _readonly_uri(db_path),
        uri=True,
        timeout=busy_timeout_ms / 1_000,
    )
    try:
        # 连接级只读保护不改变 journal mode，也不会创建数据库文件。
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        # 该整数已经过边界校验，不包含调用方自由 SQL。
        connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        yield connection
    finally:
        connection.close()


def database_is_readable(db_path: str | Path) -> bool:
    """执行最小只读健康检查，不泄漏底层连接给调用方。"""
    with readonly_connection(db_path) as connection:
        connection.execute("SELECT 1").fetchone()
    return True
