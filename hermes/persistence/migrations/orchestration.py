"""持久化任务编排四张事实表的 schema migration。"""

from __future__ import annotations

import sqlite3

from ..schemas.orchestration import create_schema


def _migrate_v38_to_v39(conn: sqlite3.Connection) -> None:
    """创建 Workflow、Task、Dependency 与 Run 表及索引。"""

    create_schema(conn)


__all__ = ["_migrate_v38_to_v39"]
