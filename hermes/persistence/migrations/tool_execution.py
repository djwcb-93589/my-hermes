"""通用工具执行记录领域的历史迁移。"""

from __future__ import annotations

import sqlite3

from ..schemas.tool_execution import create_schema


def _migrate_v25_to_v26(conn: sqlite3.Connection) -> None:
    """为工具执行恢复 Journal 创建独立表和查询索引。"""
    create_schema(conn)
