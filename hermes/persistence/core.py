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


def session_exists(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    source: str | None = None,
) -> bool:
    """检查指定来源的会话是否已经存在。"""
    query = "SELECT 1 FROM sessions WHERE id = ?"
    params: tuple[str, ...] = (session_id,)
    if source is not None:
        query += " AND source = ?"
        params = (session_id, source)

    return conn.execute(query, params).fetchone() is not None


def list_cli_sessions(
    conn: sqlite3.Connection,
    limit: int = 10,
    offset: int = 0,
) -> list[dict]:
    """列出有用户消息的 CLI 会话摘要。"""
    normalized_limit = max(1, min(100, int(limit)))
    normalized_offset = max(0, int(offset))
    rows = conn.execute(
        """
        SELECT session.id, message.content, message.timestamp
        FROM sessions AS session
        INNER JOIN messages AS message
            ON message.id = (
                SELECT latest.id
                FROM messages AS latest
                WHERE latest.session_id = session.id
                  AND latest.role = 'user'
                  AND TRIM(
                      COALESCE(latest.content, ''),
                      char(9) || char(10) || char(13) || ' '
                  ) <> ''
                ORDER BY latest.timestamp DESC, latest.id DESC
                LIMIT 1
            )
        WHERE session.source = 'cli'
        ORDER BY message.timestamp DESC, message.id DESC, session.id ASC
        LIMIT ? OFFSET ?
        """,
        (normalized_limit, normalized_offset),
    ).fetchall()

    summaries: list[dict] = []
    for session_id, content, timestamp in rows:
        preview = " ".join(str(content or "").split())
        if len(preview) > 120:
            preview = f"{preview[:117]}..."
        summaries.append({
            "session_id": session_id,
            "preview": preview,
            "timestamp": timestamp,
        })
    return summaries


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
    reasoning_content = msg.get("reasoning_content")
    if role == "assistant" and tool_calls_json and reasoning_content is None:
        # 思考模型要求后续请求原样带回该字段；普通模型的工具调用用空串兼容。
        reasoning_content = ""
    if reasoning_content is not None and not isinstance(
        reasoning_content,
        str,
    ):
        try:
            reasoning_content = str(reasoning_content)
        except Exception as exc:
            raise InvalidMessageError(
                f"reasoning_content cannot be coerced to str: {exc}"
            ) from exc
    # tool 角色必须带 tool_call_id,否则上下文里无法关联到原 tool_call
    if role == "tool" and not tool_call_id:
        raise InvalidMessageError("tool message missing tool_call_id")

    try:
        cursor = conn.execute(
            """
            INSERT INTO messages
                (session_id, role, content, tool_calls, tool_call_id,
                 reasoning_content, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                role,
                content,
                tool_calls_json,
                tool_call_id,
                reasoning_content,
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
    *,
    steer_persist_callback=None,
    steer_ids: tuple[str, ...] = (),
) -> None:
    """批量写入,在单个事务内执行。任一失败则整组回滚。"""
    if not isinstance(messages, (list, tuple)):
        raise InvalidMessageError(
            f"messages must be a list, got {type(messages).__name__}"
        )
    with transaction(conn):
        for msg in messages:
            _insert_message(conn, session_id, msg)
        if steer_ids and steer_persist_callback is not None:
            steer_persist_callback(conn, tuple(steer_ids))


def replace_tool_message_content(
    conn: sqlite3.Connection,
    session_id: str,
    tool_call_id: str,
    content: str,
) -> bool:
    """用受信任的工具执行结果替换同一调用的暂存 Tool Result。"""
    changed = conn.execute(
        """
        UPDATE messages
        SET content=?
        WHERE session_id=? AND role='tool' AND tool_call_id=?
        """,
        (str(content), str(session_id), str(tool_call_id)),
    ).rowcount
    if changed != 1:
        conn.rollback()
        return False
    conn.commit()
    return True


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
        SELECT role, content, tool_calls, tool_call_id, reasoning_content
        FROM messages
        WHERE session_id = ?
        ORDER BY id
        """,
        (session_id,),
    ).fetchall()

    return _deserialize_message_rows(rows)


def get_session_messages_in_id_range(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    after_message_id: int,
    upto_message_id: int,
) -> list[dict]:
    """读取会话内固定消息 ID 窗口，并确认窗口末端完整存在。"""
    if not isinstance(session_id, str) or not session_id.strip():
        raise InvalidMessageError("session_id is required")
    if (
        isinstance(after_message_id, bool)
        or not isinstance(after_message_id, int)
        or after_message_id < 0
    ):
        raise InvalidMessageError("after_message_id must be a non-negative integer")
    if (
        isinstance(upto_message_id, bool)
        or not isinstance(upto_message_id, int)
        or upto_message_id <= 0
    ):
        raise InvalidMessageError("upto_message_id must be a positive integer")
    if upto_message_id <= after_message_id:
        raise InvalidMessageError(
            "upto_message_id must be greater than after_message_id"
        )

    rows = conn.execute(
        """
        SELECT id, role, content, tool_calls, tool_call_id, reasoning_content
        FROM messages
        WHERE session_id = ? AND id > ? AND id <= ?
        ORDER BY id
        """,
        (session_id, after_message_id, upto_message_id),
    ).fetchall()
    if not rows or int(rows[-1][0]) != upto_message_id:
        raise InvalidMessageError("background review message window is incomplete")

    return _deserialize_message_rows([row[1:] for row in rows])


def _deserialize_message_rows(rows) -> list[dict]:
    """将消息查询行还原为公共消息字典。"""
    messages: list[dict] = []
    for role, content, tool_calls_json, tool_call_id, reasoning_content in rows:
        msg: dict = {"role": role, "content": content or ""}
        calls = _deserialize_tool_calls(tool_calls_json)
        if calls:
            msg["tool_calls"] = calls
        if tool_call_id:
            msg["tool_call_id"] = tool_call_id
        if reasoning_content is not None or (role == "assistant" and calls):
            msg["reasoning_content"] = reasoning_content or ""
        messages.append(msg)
    return messages


def get_last_session_message_id(
    conn: sqlite3.Connection,
    session_id: str,
) -> int | None:
    """返回会话最后一条已持久化消息的数据库 ID。"""
    row = conn.execute(
        "SELECT MAX(id) FROM messages WHERE session_id=?",
        (session_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


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
        SELECT m.role, m.content, m.tool_calls, m.tool_call_id,
               m.reasoning_content
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
    for role, content, tool_calls_json, tool_call_id, reasoning_content in rows:
        msg: dict = {"role": role, "content": content or ""}
        calls = _deserialize_tool_calls(tool_calls_json)
        if calls:
            msg["tool_calls"] = calls
        if tool_call_id:
            msg["tool_call_id"] = tool_call_id
        if reasoning_content is not None or (role == "assistant" and calls):
            msg["reasoning_content"] = reasoning_content or ""
        messages.append(msg)
    return messages


def add_model_call_event(
    conn: sqlite3.Connection,
    session_id: str,
    event: dict,
) -> None:
    """写入不含提示词、回答和推理正文的模型调用诊断。"""
    fields = (
        "iteration", "model", "model_role", "outcome", "finish_reason",
        "latency_ms", "has_content", "content_chars", "has_reasoning",
        "reasoning_chars", "tool_call_count", "prompt_tokens",
        "completion_tokens", "total_tokens", "reasoning_tokens",
        "cached_tokens", "http_status", "error_category",
        "exception_type",
    )
    values = [event.get(field) for field in fields]
    conn.execute(
        f"""
        INSERT INTO model_call_events
            (session_id, {', '.join(fields)}, created_at)
        VALUES ({', '.join('?' for _ in range(len(fields) + 2))})
        """,
        (session_id, *values, time.time()),
    )
    conn.commit()


def list_model_call_events(
    conn: sqlite3.Connection,
    session_id: str,
) -> list[dict]:
    """按调用顺序返回脱敏模型诊断记录。"""
    rows = conn.execute(
        "SELECT * FROM model_call_events WHERE session_id=? ORDER BY id",
        (session_id,),
    ).fetchall()
    columns = [
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(model_call_events)"
        ).fetchall()
    ]
    return [dict(zip(columns, row)) for row in rows]

