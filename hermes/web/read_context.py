"""Dashboard 读取服务共享的 SQLite 只读访问边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from hermes.persistence.database import DBError
from hermes.persistence.read_only import readonly_connection


class DashboardReadError(Exception):
    """不会向 HTTP 响应泄漏底层细节的 Dashboard 读取异常。"""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class ReadDataUnavailable(DashboardReadError):
    """当前无法安全完成只读查询。"""

    _REASON_CODES = frozenset({
        "database_unavailable",
        "database_busy",
        "data_invalid",
        "data_unavailable",
        "catalog_unavailable",
    })

    def __init__(self, reason_code: str = "data_unavailable"):
        super().__init__(
            reason_code if reason_code in self._REASON_CODES else "data_unavailable"
        )


class ResourceNotFound(DashboardReadError):
    """读取目标不存在。"""

    def __init__(self, _message: str | None = None):
        """兼容旧调用方传入消息，但不把它暴露到 HTTP 响应。"""
        del _message
        super().__init__("resource_not_found")


class DashboardReadContext:
    """保存规范化数据库路径，并统一使用现有只读连接协议。"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = str(db_path) if isinstance(db_path, Path) else db_path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """打开 mode=ro、query_only 的连接并映射可预期读取失败。"""
        if not isinstance(self.db_path, str) or not self.db_path.strip():
            raise ReadDataUnavailable("database_unavailable")
        opened = False
        try:
            with readonly_connection(self.db_path) as conn:
                opened = True
                yield conn
        except sqlite3.Error as exc:
            raise ReadDataUnavailable(_sqlite_reason_code(exc)) from exc
        except DBError as exc:
            raise ReadDataUnavailable("data_invalid") from exc
        except (OSError, ValueError) as exc:
            if not opened:
                raise ReadDataUnavailable("database_unavailable") from exc
            raise


def _sqlite_reason_code(exc: sqlite3.Error) -> str:
    """区分暂时的锁竞争与其他不可用数据库状态。"""
    message = str(exc).lower()
    if "locked" in message or "busy" in message:
        return "database_busy"
    return "database_unavailable"
