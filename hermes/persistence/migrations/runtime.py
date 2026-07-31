"""Runtime Component 当前快照表的 schema 迁移。"""

from __future__ import annotations

import sqlite3

from ..schemas.runtime import create_schema


def _migrate_v36_to_v37(conn: sqlite3.Connection) -> None:
    """创建 Runtime 当前状态表及三个低基数读取索引。"""
    create_schema(conn)


__all__ = ["_migrate_v36_to_v37"]
