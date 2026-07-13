"""
SQLite 持久化层。

集中处理:schema version 管理、PRAGMA、外键约束、索引、事务原子性、
JSON 序列化 / 反序列化。上层调用方不应直接 ``json.dumps(tool_calls)``,
统一走 ``add_message`` / ``get_session_messages``。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


# ===========================================================================
# schema version / migration
# ===========================================================================

# 当前最新 schema 版本。每次升级表结构时 +1,并在 _migrate 里加对应分支。
# 为什么需要 schema version:让 db 启动时知道结构处于哪个版本,需要的话
# 按顺序执行 migration,避免依赖用户手动删库升级。
LATEST_SCHEMA_VERSION = 3

# 允许的 role 白名单。非法 role 显式报错,不静默吞掉。
_ALLOWED_ROLES = {"user", "assistant", "system", "tool"}


class DBError(Exception):
    """db 层错误基类。"""


class InvalidMessageError(DBError):
    """消息结构非法(role 不允许 / 缺 session_id / 缺 tool_call_id / JSON
    序列化失败等)。"""


# ===========================================================================
# PRAGMA
# ===========================================================================

def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """每个连接初始化时设置 PRAGMA。"""
    # foreign_keys=ON 让 messages.session_id → sessions.id 外键约束生效,
    # 不存在的 session_id 不能插入 message。
    conn.execute("PRAGMA foreign_keys = ON")
    # busy_timeout:并发写入时等待 5s 而非立即抛 SQLITE_BUSY。
    conn.execute("PRAGMA busy_timeout = 5000")
    # WAL 模式:读写不互斥,适合一写多读。会生成 .db-wal / .db-shm。
    conn.execute("PRAGMA journal_mode = WAL")


# ===========================================================================
# schema 定义
# ===========================================================================

def _create_latest_schema(conn: sqlite3.Connection) -> None:
    """直接建最新版 schema(全新库走这条)。"""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            started_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_calls TEXT,
            tool_call_id TEXT,
            timestamp REAL NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        -- 为什么需要这个 index:get_session_messages 按 session_id 过滤、
        -- 按 id 排序。复合索引 (session_id, id) 让该查询稳定高效。
        CREATE INDEX IF NOT EXISTS idx_messages_session_order
            ON messages(session_id, id);

        -- Gateway 的 route_key 稳定不变,/new 只切换 conversation_id。
        -- 单独持久化当前映射,让进程重启后仍能恢复到最新会话。
        CREATE TABLE IF NOT EXISTS gateway_session_routes (
            route_key TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        """
    )


def _get_schema_version(conn: sqlite3.Connection) -> int:
    """读取当前 schema version。

    返回 0 表示全新库(还没任何表);返回 1 表示老库(v1 时代还没引入
    schema_version 表);返回 >=2 表示已经过 migration。
    """
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    )
    if cur.fetchone() is None:
        # schema_version 表不存在 —— 可能是全新库,也可能是 v1 老库
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
        )
        if cur.fetchone() is None:
            return 0  # 全新库
        return 1  # v1 老库(只有 sessions/messages,无 schema_version 表)
    row = conn.execute(
        "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else 0


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    # schema_version 只保留一行,避免未来版本号堆叠导致判断含糊。
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _count_rows(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])


def _validate_v1_data(conn: sqlite3.Connection) -> None:
    """校验 v1 旧数据能否无损迁移到最新 schema。"""
    if not _table_exists(conn, "sessions") or not _table_exists(conn, "messages"):
        raise DBError("v1 db missing sessions/messages table")

    bad_sessions = _count_rows(
        conn,
        """
        SELECT COUNT(*)
        FROM sessions
        WHERE id IS NULL OR started_at IS NULL
        """,
    )
    if bad_sessions:
        raise DBError(
            f"cannot migrate v1 db: {bad_sessions} invalid session rows"
        )

    bad_messages = _count_rows(
        conn,
        """
        SELECT COUNT(*)
        FROM messages AS m
        LEFT JOIN sessions AS s ON s.id = m.session_id
        WHERE m.session_id IS NULL
           OR m.role IS NULL
           OR m.timestamp IS NULL
           OR s.id IS NULL
        """,
    )
    if bad_messages:
        raise DBError(
            f"cannot migrate v1 db: {bad_messages} invalid or orphan messages"
        )


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """v1 → v2:重建表,让旧库也获得 NOT NULL / 外键约束。"""
    _validate_v1_data(conn)

    backup_tables = ("sessions_v1_backup", "messages_v1_backup")
    if any(_table_exists(conn, table) for table in backup_tables):
        raise DBError("cannot migrate v1 db: leftover migration backup table")

    conn.execute("ALTER TABLE sessions RENAME TO sessions_v1_backup")
    conn.execute("ALTER TABLE messages RENAME TO messages_v1_backup")

    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            started_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_calls TEXT,
            tool_call_id TEXT,
            timestamp REAL NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        INSERT INTO sessions (id, source, started_at)
        SELECT id, source, started_at
        FROM sessions_v1_backup
        """
    )
    conn.execute(
        """
        INSERT INTO messages
            (id, session_id, role, content, tool_calls, tool_call_id, timestamp)
        SELECT id, session_id, role, content, tool_calls, tool_call_id, timestamp
        FROM messages_v1_backup
        """
    )

    conn.execute("DROP TABLE messages_v1_backup")
    conn.execute("DROP TABLE sessions_v1_backup")


def _migrate(conn: sqlite3.Connection, current: int) -> int:
    """按版本号顺序执行 migration,返回最新版本。

    老库 v1 → v2 会重建 sessions/messages,让外键 / NOT NULL
    约束对既有数据库也生效。v2 → v3 新增 Gateway 当前会话映射。
    旧数据不满足新约束时拒绝迁移。
    """
    if current < 1:
        # 极少见:有 schema_version 表但版本 < 1,补基础表
        _create_latest_schema(conn)
        current = LATEST_SCHEMA_VERSION

    if current < 2:
        # DDL 不依赖 sqlite3.Connection 的 with 协议,这里显式开事务,
        # 避免重建表中途失败时留下半迁移状态。
        conn.execute("BEGIN")
        try:
            _migrate_v1_to_v2(conn)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version("
                "version INTEGER PRIMARY KEY)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_session_order "
                "ON messages(session_id, id)"
            )
            _set_schema_version(conn, 2)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 2

    if current < 3:
        conn.execute("BEGIN")
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_session_routes (
                    route_key TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            _set_schema_version(conn, 3)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 3

    return current


# ===========================================================================
# init_db
# ===========================================================================

def init_db(db_path: str) -> sqlite3.Connection:
    """初始化 db:建父目录、应用 PRAGMA、检查 schema version 并按需 migration。

    全新库直接建最新 schema;老库按序 migration。返回 sqlite3 连接。
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        _apply_pragmas(conn)

        current = _get_schema_version(conn)
        if current > LATEST_SCHEMA_VERSION:
            raise DBError(
                "db schema version is newer than this code supports: "
                f"{current} > {LATEST_SCHEMA_VERSION}"
            )
        if current == 0:
            # 全新库:直接建最新 schema
            _create_latest_schema(conn)
            _set_schema_version(conn, LATEST_SCHEMA_VERSION)
        elif current < LATEST_SCHEMA_VERSION:
            _migrate(conn, current)

        conn.commit()
        return conn
    except Exception:
        conn.close()
        raise


# ===========================================================================
# 序列化 / 反序列化(集中处理,上层不直接 json.dumps)
# ===========================================================================

def _serialize_tool_calls(tool_calls) -> str | None:
    """把 tool_calls 序列化成 db 字符串。

    None / 空列表 → NULL(避免反序列化时把 [] 当成有意义数据)。
    JSON 失败抛 InvalidMessageError,不静默吞掉。
    """
    if tool_calls is None:
        return None
    if not isinstance(tool_calls, list):
        raise InvalidMessageError(
            f"tool_calls must be a list, got {type(tool_calls).__name__}"
        )
    if not tool_calls:
        return None
    try:
        return json.dumps(tool_calls)
    except (TypeError, ValueError) as exc:
        raise InvalidMessageError(
            f"tool_calls JSON serialization failed: {exc}"
        ) from exc


def _deserialize_tool_calls(s: str | None):
    """从 db 字符串还原 tool_calls。失败抛 InvalidMessageError。"""
    if not s:
        return None
    try:
        return json.loads(s)
    except ValueError as exc:
        raise InvalidMessageError(
            f"tool_calls JSON deserialization failed: {exc}"
        ) from exc


# ===========================================================================
# 事务上下文管理器
# ===========================================================================

@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """事务上下文:进入时开事务,正常退出 COMMIT,异常 ROLLBACK。

    为什么 add_messages 要用事务:批量写入一组消息时,如果中间某条
    失败(结构非法 / 序列化失败 / 外键约束 / 磁盘满等),整组必须回滚,
    不能留下半截消息破坏历史一致性。

    注意:工具执行失败不是事务失败 —— 那种情况应该把错误包装成 tool
    message 正常写入上下文和数据库(让模型看到工具错误)。事务回滚
    只针对真正持久化失败场景。
    """
    # sqlite3.Connection 的 with 协议:__enter__ 返回 self,正常退出
    # commit,异常 rollback。直接复用最简洁,避免与 Python 自动事务
    # 管理冲突。
    with conn:
        yield


# ===========================================================================
# CRUD
# ===========================================================================

def create_session(conn: sqlite3.Connection, source: str = "cli") -> str:
    """创建新 session,返回其 ID。"""
    session_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
        (session_id, source, time.time()),
    )
    conn.commit()
    return session_id


def ensure_session(
    conn: sqlite3.Connection,
    session_id: str,
    source: str = "gateway",
) -> None:
    """确保 session 存在,不存在则创建。Gateway / runner 统一走这个入口,
    不在上层自行拼 SQL。
    """
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
        (session_id, source, time.time()),
    )
    conn.commit()


def get_gateway_conversation_id(
    conn: sqlite3.Connection,
    route_key: str,
) -> str | None:
    """读取 route_key 当前指向的 conversation_id。没有映射时返回 None。"""
    row = conn.execute(
        "SELECT conversation_id FROM gateway_session_routes WHERE route_key=?",
        (route_key,),
    ).fetchone()
    return str(row[0]) if row else None


def set_gateway_conversation_id(
    conn: sqlite3.Connection,
    route_key: str,
    conversation_id: str,
) -> None:
    """持久化 route_key 当前指向的 conversation_id。"""
    conn.execute(
        """
        INSERT INTO gateway_session_routes
            (route_key, conversation_id, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(route_key) DO UPDATE SET
            conversation_id=excluded.conversation_id,
            updated_at=excluded.updated_at
        """,
        (route_key, conversation_id, time.time()),
    )
    conn.commit()


def _insert_message(conn: sqlite3.Connection, session_id: str, msg: dict) -> None:
    """实际 INSERT 一行,不 commit(供 add_message / add_messages 复用)。"""
    if not session_id:
        raise InvalidMessageError("session_id is required")
    if not isinstance(msg, dict):
        raise InvalidMessageError(
            f"msg must be a dict, got {type(msg).__name__}"
        )

    role = msg.get("role")
    if role not in _ALLOWED_ROLES:
        raise InvalidMessageError(
            f"invalid role: {role!r}; allowed: {sorted(_ALLOWED_ROLES)}"
        )

    # content 统一成字符串(None / 缺失 → "")
    content = msg.get("content", "")
    if content is None:
        content = ""
    if not isinstance(content, str):
        # 非 str 内容尝试字符串化,失败则报错
        try:
            content = str(content)
        except Exception as exc:
            raise InvalidMessageError(
                f"content cannot be coerced to str: {exc}"
            ) from exc

    # tool_calls 序列化(集中处理,失败抛 InvalidMessageError)
    tool_calls_json = _serialize_tool_calls(msg.get("tool_calls"))

    tool_call_id = msg.get("tool_call_id")
    # tool 角色必须带 tool_call_id,否则上下文里无法关联到原 tool_call
    if role == "tool" and not tool_call_id:
        raise InvalidMessageError("tool message missing tool_call_id")

    try:
        conn.execute(
            """
            INSERT INTO messages
                (session_id, role, content, tool_calls, tool_call_id, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                role,
                content,
                tool_calls_json,
                tool_call_id,
                time.time(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        # 外键约束失败(session_id 不存在)/ NOT NULL 违反 等
        raise InvalidMessageError(f"db integrity error: {exc}") from exc


def add_message(
    conn: sqlite3.Connection,
    session_id: str,
    msg: dict,
) -> None:
    """单条写入 + commit。保持向后兼容。

    失败抛 InvalidMessageError,调用方需要时显式 try/except。
    """
    _insert_message(conn, session_id, msg)
    conn.commit()


def add_messages(
    conn: sqlite3.Connection,
    session_id: str,
    messages: list[dict],
) -> None:
    """批量写入,在单个事务内执行。任一失败则整组回滚。"""
    if not isinstance(messages, (list, tuple)):
        raise InvalidMessageError(
            f"messages must be a list, got {type(messages).__name__}"
        )
    with transaction(conn):
        for msg in messages:
            _insert_message(conn, session_id, msg)


def get_session_messages(
    conn: sqlite3.Connection,
    session_id: str,
) -> list[dict]:
    """按插入顺序返回某 session 的全部消息。

    反序列化在 db 层内完成:tool_calls 字段从 JSON 字符串还原成 list/dict,
    上层调用方不应再处理 JSON。
    """
    rows = conn.execute(
        """
        SELECT role, content, tool_calls, tool_call_id
        FROM messages
        WHERE session_id = ?
        ORDER BY id
        """,
        (session_id,),
    ).fetchall()

    messages: list[dict] = []
    for role, content, tool_calls_json, tool_call_id in rows:
        msg: dict = {"role": role, "content": content or ""}
        # tool_calls 反序列化(集中处理)
        calls = _deserialize_tool_calls(tool_calls_json)
        if calls:
            msg["tool_calls"] = calls
        if tool_call_id:
            msg["tool_call_id"] = tool_call_id
        messages.append(msg)
    return messages


# 别名:spec 描述时用了 get_messages,这里暴露同义名称
get_messages = get_session_messages
