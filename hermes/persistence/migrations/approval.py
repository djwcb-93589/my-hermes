"""Gateway 审批领域的历史迁移。"""

from __future__ import annotations

import sqlite3

from ..schemas.approval import _create_gateway_approval_schema


def _migrate_v13_to_v14(conn: sqlite3.Connection) -> None:
    _create_gateway_approval_schema(conn)
