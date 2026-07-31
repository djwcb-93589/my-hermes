"""Backend Control 持久请求与进程绑定的 schema 迁移。"""

from __future__ import annotations

import sqlite3

from ..schemas.backend_control import create_schema


def _migrate_v37_to_v38(conn: sqlite3.Connection) -> None:
    """创建 Gateway Supervisor 的两张最小控制表。"""
    create_schema(conn)


__all__ = ["_migrate_v37_to_v38"]
