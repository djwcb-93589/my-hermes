"""为受控管理操作打开已有 SQLite 数据库的写连接。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def _existing_write_uri(db_path: str | Path) -> str:
    """生成只允许打开既有文件的 SQLite 写入 URI。"""
    return f"{Path(db_path).absolute().as_uri()}?mode=rw"


@contextmanager
def existing_write_connection(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """为一次受控操作打开并关闭独立写连接。"""
    connection = sqlite3.connect(_existing_write_uri(db_path), uri=True)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        yield connection
    finally:
        connection.close()
