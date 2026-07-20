from __future__ import annotations

import sqlite3
import time
import uuid

from .database import (
    InvalidMessageError,
    _ALLOWED_ROLES,
    _deserialize_tool_calls,
    _serialize_tool_calls,
    transaction,
)

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


def _insert_message(conn: sqlite3.Connection, session_id: str, msg: dict) -> int:
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
        cursor = conn.execute(
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
    return int(cursor.lastrowid)


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


get_messages = get_session_messages


def get_gateway_visible_session_messages(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    exclude_approval_placeholders: bool = False,
) -> list[dict]:
    """读取 Gateway 用户实际可见的历史，不改变普通 CLI 的读取语义。

    只有与投递记录关联的最终 assistant 回答会被检查状态；没有关联的旧记录
    默认按已送达处理，tool-call/continuation 等内部消息也会完整保留。

    ``exclude_approval_placeholders=True`` 时排除审批问题占位消息：这类
    消息由 Gateway 注入而非 LLM 生成，留在审批恢复历史里会让 LLM 误以为
    上一轮自己在请求审批，进而重复催促用户回复 /approve。
    """
    approval_filter = ""
    if exclude_approval_placeholders:
        approval_filter = """
          AND NOT EXISTS (
              SELECT 1
              FROM gateway_message_deliveries AS d
              JOIN gateway_outbox AS o ON o.id = d.delivery_id
              WHERE d.assistant_message_id = m.id
                AND (
                    o.delivery_kind = 'approval_request'
                    OR o.delivery_kind LIKE 'approval_request:%'
                )
          )
        """
    rows = conn.execute(
        f"""
        SELECT m.role, m.content, m.tool_calls, m.tool_call_id
        FROM messages AS m
        WHERE m.session_id = ?
          AND (
              m.role != 'assistant'
              OR NOT EXISTS (
                  SELECT 1
                  FROM gateway_message_deliveries AS delivery
                  WHERE delivery.assistant_message_id = m.id
                    AND delivery.status != 'delivered'
              )
          )
          {approval_filter}
        ORDER BY m.id
        """,
        (session_id,),
    ).fetchall()

    messages: list[dict] = []
    for role, content, tool_calls_json, tool_call_id in rows:
        msg: dict = {"role": role, "content": content or ""}
        calls = _deserialize_tool_calls(tool_calls_json)
        if calls:
            msg["tool_calls"] = calls
        if tool_call_id:
            msg["tool_call_id"] = tool_call_id
        messages.append(msg)
    return messages

