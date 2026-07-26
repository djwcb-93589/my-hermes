"""Background Review 状态领域的 schema 迁移。"""

from __future__ import annotations

import sqlite3

from ..schemas.background_review import create_schema


def _migrate_v32_to_v33(conn: sqlite3.Connection) -> None:
    """为已有数据库创建 Background Review 状态表。"""
    create_schema(conn)
