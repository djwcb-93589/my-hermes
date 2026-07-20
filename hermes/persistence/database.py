"""SQLite 持久化基础能力。

本模块只承载与具体业务领域无关的数据库基础设施:

* DB 异常类型;
* PRAGMA 与表结构 helper;
* 通用 JSON 序列化 / 反序列化;
* 事务上下文 ``transaction``;
* 各领域共享的状态常量与默认值;
* Feishu 路由键编码(被 feishu 领域与 schema migration 共享,放本模块
  避免领域层与 migration 之间出现循环依赖)。

schema version、建表与迁移由 ``hermes.persistence.schema`` 统一负责,
本模块不得反向依赖 ``schema`` 领域,以保持单向依赖:

    schema -> database
    gateway / delivery / approval / cron / feishu -> database

业务领域模块 (Gateway、Approval、Cron、Delivery) 的状态机和跨表
协调逻辑不允许下沉到本模块。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Iterator


# ===========================================================================
# 状态常量
# ===========================================================================

# 允许的 role 白名单。非法 role 显式报错,不静默吞掉。
_ALLOWED_ROLES = {"user", "assistant", "system", "tool"}

GATEWAY_APPROVAL_STATUSES = frozenset({
    "pending",
    "executing",
    "executed",
    "denied",
    "expired",
    "cancelled",
    "failed",
    "execution_unknown",
})

GATEWAY_FILE_DELIVERY_STATUSES = frozenset({
    "pending",
    "uploading",
    "uploaded",
    "retry_wait",
    "outbox_created",
    "delivered",
    "cancelled",
    "permanent_failed",
})

# Feishu Inbox 状态由数据库层统一约束,Adapter 只使用这里暴露的访问接口。
FEISHU_INBOX_STATUSES = frozenset({
    "pending",
    "processing",
    "retry_wait",
    "processed",
    "cancelled",
    "permanent_failed",
})

CRON_SCHEDULE_TYPES = frozenset({"one_shot", "interval", "cron"})
CRON_OVERLAP_POLICIES = frozenset({"skip", "queue", "allow", "parallel"})
CRON_MISFIRE_POLICIES = frozenset({"skip", "run_once", "catch_up"})
CRON_APPROVAL_STATUSES = frozenset({
    "not_required",
    "pending",
    "granted",
    "denied",
    "expired",
    "revoked",
})
CRON_RUN_STATUSES = frozenset({
    "claimed",
    "running",
    "completed",
    "failed",
    "blocked",
    "cancelled",
})
CRON_RUN_TRANSITIONS = {
    "claimed": frozenset({"running", "failed", "blocked", "cancelled"}),
    "running": frozenset({"completed", "failed", "blocked", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "blocked": frozenset(),
    "cancelled": frozenset(),
}

CRON_DELIVERY_STATUSES = frozenset({
    "not_requested", "preparation_pending", "preparing", "pending",
    "delivered", "partial_failed", "permanent_failed", "invalid_target",
})

# Approval 领域共享的 AgentLoop 最小可持久化状态。保留在 database 是因为
# 多个领域 (Approval、Cron 恢复) 都需要相同的默认形状,移到任一具体领域
# 都会让其他领域反向依赖它。
_DEFAULT_GATEWAY_APPROVAL_AGENT_STATE = {
    "iterations_used": 0,
    "retry_count": 0,
    "continuation_count": 0,
    "using_fallback": False,
    "active_model": "",
}


# ===========================================================================
# 异常
# ===========================================================================


class DBError(Exception):
    """db 层错误基类。"""


class InvalidMessageError(DBError):
    """消息结构非法(role 不允许 / 缺 session_id / 缺 tool_call_id / JSON
    序列化失败等)。"""


class InvalidFeishuInboxPayloadError(DBError):
    """Feishu Inbox payload 已损坏或不是 JSON 对象。"""


# ===========================================================================
# PRAGMA / 表结构 helper
# ===========================================================================


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """每个连接初始化时设置 PRAGMA。"""
    # foreign_keys=ON 让 messages.session_id -> sessions.id 外键约束生效,
    # 不存在的 session_id 不能插入 message。
    conn.execute("PRAGMA foreign_keys = ON")
    # busy_timeout:并发写入时等待 5s 而非立即抛 SQLITE_BUSY。
    conn.execute("PRAGMA busy_timeout = 5000")
    # WAL 模式:读写不互斥,适合一写多读。会生成 .db-wal / .db-shm。
    conn.execute("PRAGMA journal_mode = WAL")


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    """返回表的列名集合,仅供受控的 schema migration 使用。"""
    return {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def _count_rows(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])


# ===========================================================================
# 通用序列化 / 校验 helper
# ===========================================================================


def _serialize_tool_calls(tool_calls) -> str | None:
    """把 tool_calls 序列化成 db 字符串。

    None / 空列表 -> NULL(避免反序列化时把 [] 当成有意义数据)。
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


def _cleanup_batch_limit(value, label: str) -> int:
    """统一校验维护操作的批次上限,避免非法值进入 SQL LIMIT。"""
    if isinstance(value, bool):
        raise DBError(f"{label} cleanup limit must be positive")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise DBError(f"{label} cleanup limit must be positive") from exc
    if normalized <= 0:
        raise DBError(f"{label} cleanup limit must be positive")
    return normalized


# ===========================================================================
# Feishu 路由键编码(跨 feishu 领域与 schema migration 共享)
# ===========================================================================


def build_feishu_inbox_route_key(
    account_id: str,
    chat_type: str,
    chat_id: str,
    user_id: str,
    thread_id: str | None,
) -> str:
    """编码无歧义的 Feishu Inbox 路由身份。"""
    normalized_account_id = str(account_id or "")
    if not normalized_account_id:
        raise DBError("Feishu Inbox account_id must not be empty")
    identity = (
        "feishu",
        normalized_account_id,
        str(chat_type or ""),
        str(chat_id or ""),
        str(user_id or ""),
        str(thread_id or ""),
    )
    return json.dumps(identity, ensure_ascii=False, separators=(",", ":"))


def _derive_feishu_inbox_route_key(
    app_id: str,
    message_id: str,
    encoded_payload: str,
) -> str:
    """从旧 Inbox payload 回填路由;不可识别记录进入独立隔离路由。"""
    try:
        payload = json.loads(encoded_payload)
        event = payload.get("event", {})
        message = event.get("message", {})
        sender = event.get("sender", {})
        sender_ids = sender.get("sender_id", {})
        if not all(
            isinstance(value, dict)
            for value in (payload, event, message, sender, sender_ids)
        ):
            raise ValueError("invalid Feishu Inbox route payload")
        chat_id = str(message.get("chat_id", "") or "")
        user_id = str(
            sender_ids.get("open_id")
            or sender_ids.get("user_id")
            or ""
        )
        if not chat_id or not user_id:
            raise ValueError("incomplete Feishu Inbox route identity")
        raw_chat_type = str(message.get("chat_type", "p2p") or "p2p")
        chat_type = "dm" if raw_chat_type == "p2p" else raw_chat_type
        thread_id = message.get("thread_id") or message.get("root_id")
        return build_feishu_inbox_route_key(
            app_id,
            chat_type,
            chat_id,
            user_id,
            str(thread_id) if thread_id else None,
        )
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        # 旧损坏数据无法可靠恢复真实路由,只能按消息隔离;新写入会在 ACK
        # 前保存从已校验 MessageEvent 得到的 route_key,不存在这一信息缺口。
        return build_feishu_inbox_route_key(
            app_id,
            "invalid",
            f"inbox:{message_id}",
            "",
            None,
        )


# ===========================================================================
# 事务
# ===========================================================================


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """事务上下文:进入时开事务,正常退出 COMMIT,异常 ROLLBACK。

    为什么 add_messages 要用事务:批量写入一组消息时,如果中间某条
    失败(结构非法 / 序列化失败 / 外键约束 / 磁盘满等),整组必须回滚,
    不能留下半截消息破坏历史一致性。

    注意:工具执行失败不是事务失败 -- 那种情况应该把错误包装成 tool
    message 正常写入上下文和数据库(让模型看到工具错误)。事务回滚
    只针对真正持久化失败场景。
    """
    # sqlite3.Connection 的 with 协议:__enter__ 返回 self,正常退出
    # commit,异常 rollback。直接复用最简洁,避免与 Python 自动事务
    # 管理冲突。
    with conn:
        yield


@contextmanager
def _immediate_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """以写锁开始短事务,供 lease 竞争、探针和有界清理使用。

    与 ``transaction`` 的区别:``BEGIN IMMEDIATE`` 在事务开始时就获取
    写锁,让并发写入者排队等待,而不是在第一次写入时才冲突。需要 fencing
    的操作(lease 竞争、有界清理、状态探针)用它,避免与其他写事务冲突
    后才知道失败。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
