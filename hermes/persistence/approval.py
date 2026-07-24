from __future__ import annotations

import json
import math
import re
import sqlite3
import time
import uuid

from hermes.approval_policy import (
    approval_request_binding_matches, emit_approval_audit, is_grant_scope_allowed,
)
from .database import (
    DBError, InvalidMessageError, _DEFAULT_GATEWAY_APPROVAL_AGENT_STATE, transaction,
)
from .core import _insert_message
from .delivery import _insert_gateway_message_delivery
from .gateway import (
    _enqueue_gateway_message_in_transaction, _insert_gateway_outbox,
    _serialize_gateway_json, _set_gateway_queue_status,
    gateway_event_source_message_ids,
)

_GATEWAY_APPROVAL_COLUMNS = (
    "id, route_key, conversation_id, requester_user_id, source_message_id, "
    "tool_call_id, tool_message_id, tool_name, tool_args_json, summary, "
    "details_json, status, decision_message_id, result_content, "
    "source_event_json, agent_state_json, created_at, expires_at, updated_at, "
    "grant_scope, execution_started"
)

_TOOL_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,127}")

def _normalize_gateway_approval_agent_state(value: dict | None) -> dict:
    """校验并收敛可持久化的最小 AgentLoop 状态。"""
    state = dict(_DEFAULT_GATEWAY_APPROVAL_AGENT_STATE)
    if value is not None:
        if not isinstance(value, dict):
            raise DBError("gateway approval agent state must contain an object")
        state.update(value)

    for field in ("iterations_used", "retry_count", "continuation_count"):
        raw = state.get(field)
        if isinstance(raw, bool):
            raise DBError(f"gateway approval agent state {field} is invalid")
        try:
            normalized = int(raw)
        except (TypeError, ValueError) as exc:
            raise DBError(
                f"gateway approval agent state {field} is invalid"
            ) from exc
        if normalized < 0:
            raise DBError(f"gateway approval agent state {field} is invalid")
        state[field] = normalized

    if not isinstance(state.get("using_fallback"), bool):
        raise DBError("gateway approval fallback state is invalid")
    if not isinstance(state.get("active_model"), str):
        raise DBError("gateway approval active model is invalid")
    return {
        "iterations_used": state["iterations_used"],
        "retry_count": state["retry_count"],
        "continuation_count": state["continuation_count"],
        "using_fallback": state["using_fallback"],
        "active_model": state["active_model"],
    }


def _gateway_approval_row(row) -> dict | None:
    """把审批查询行还原为上层可使用的结构。"""
    if row is None:
        return None
    try:
        tool_args = json.loads(row[8])
        details = json.loads(row[10])
        agent_state = json.loads(row[15])
    except (TypeError, ValueError) as exc:
        raise DBError(f"gateway approval JSON deserialization failed: {exc}") from exc
    if not isinstance(tool_args, dict) or not isinstance(details, dict):
        raise DBError("gateway approval JSON has invalid structure")
    agent_state = _normalize_gateway_approval_agent_state(agent_state)
    fingerprint = details.get("fingerprint")
    return {
        "id": str(row[0]),
        "route_key": str(row[1]),
        "conversation_id": str(row[2]),
        "session_key": str(row[2]),
        "requester_user_id": str(row[3]),
        "source_message_id": str(row[4]),
        "tool_call_id": str(row[5]),
        "tool_message_id": int(row[6]),
        "tool_name": str(row[7]),
        "tool_args": tool_args,
        "summary": str(row[9]),
        "details": details,
        "fingerprint": str(fingerprint) if fingerprint is not None else "",
        "status": str(row[11]),
        "decision_message_id": (
            str(row[12]) if row[12] is not None else None
        ),
        "result_content": (
            str(row[13]) if row[13] is not None else None
        ),
        "source_event_json": (
            str(row[14]) if row[14] is not None else None
        ),
        "agent_state": agent_state,
        "created_at": float(row[16]),
        "expires_at": float(row[17]),
        "updated_at": float(row[18]),
        "grant_scope": str(row[19]) if row[19] is not None else None,
        "execution_started": bool(row[20]),
    }


def _approval_terminal_content(request_id: str, status: str) -> str:
    """生成拒绝、过期等未执行审批的最终 Tool Result。"""
    return json.dumps(
        {
            "ok": False,
            "error_type": f"approval_{status}",
            "approval_request_id": request_id,
            "error": f"Tool operation was not executed: approval {status}.",
        },
        ensure_ascii=False,
    )


def _emit_gateway_approval_audit(
    request: dict,
    *,
    decision: str,
    grant_scope: str | None,
    decision_source: str,
    timestamp: float,
) -> None:
    """从持久请求提取安全审计字段，不读取或输出 tool_args。"""
    details = request.get("details", {})
    emit_approval_audit(
        request_id=request.get("id"),
        session_key=request.get("conversation_id"),
        tool_name=request.get("tool_name"),
        risk_level=(
            details.get("risk_level")
            if isinstance(details, dict)
            else "unknown"
        ),
        reason=(
            details.get("reason")
            if isinstance(details, dict)
            else "approval state transition"
        ),
        decision=decision,
        grant_scope=grant_scope,
        decision_source=decision_source,
        timestamp=timestamp,
    )


def _expire_gateway_approvals_in_transaction(
    conn: sqlite3.Connection,
    now: float,
) -> int:
    """在调用方事务内把超时请求转成终态，并同步 Tool Result。"""
    rows = conn.execute(
        f"""
        SELECT {_GATEWAY_APPROVAL_COLUMNS}
        FROM gateway_approval_requests
        WHERE status='pending' AND expires_at<=?
        """,
        (now,),
    ).fetchall()
    requests = [_gateway_approval_row(row) for row in rows]
    for request in requests:
        conn.execute(
            "UPDATE messages SET content=? WHERE id=?",
            (
                _approval_terminal_content(request["id"], "expired"),
                request["tool_message_id"],
            ),
        )
    if rows:
        conn.execute(
            """
            UPDATE gateway_approval_requests
            SET status='expired', updated_at=?
            WHERE status='pending' AND expires_at<=?
            """,
            (now, now),
        )
    for request in requests:
        _emit_gateway_approval_audit(
            request,
            decision="expired",
            grant_scope=None,
            decision_source="timeout",
            timestamp=now,
        )
    return len(requests)


def expire_gateway_approvals(
    conn: sqlite3.Connection,
    now: float | None = None,
) -> int:
    """公开的审批过期收敛入口。"""
    effective_now = time.time() if now is None else float(now)
    with transaction(conn):
        return _expire_gateway_approvals_in_transaction(conn, effective_now)


def recover_gateway_approvals(conn: sqlite3.Connection) -> dict:
    """启动恢复：未开始的执行保留给队列，已开始或失去队列的执行记为未知。"""
    now = time.time()
    with transaction(conn):
        expired = _expire_gateway_approvals_in_transaction(conn, now)
        rows = conn.execute(
            f"""
            SELECT {_GATEWAY_APPROVAL_COLUMNS}
            FROM gateway_approval_requests
            WHERE status='executing'
              AND (
                  execution_started=1
                  OR NOT EXISTS (
                      SELECT 1
                      FROM gateway_message_queue AS queue
                      WHERE queue.route_key=gateway_approval_requests.route_key
                        AND queue.task_kind='approval_resume'
                        AND queue.approval_id=gateway_approval_requests.id
                        AND queue.status IN ('queued', 'processing')
                  )
              )
            """
        ).fetchall()
        requests = [_gateway_approval_row(row) for row in rows]
        for request in requests:
            conn.execute(
                "UPDATE messages SET content=? WHERE id=?",
                (
                    _approval_terminal_content(
                        request["id"],
                        "execution_unknown",
                    ),
                    request["tool_message_id"],
                ),
            )
        if rows:
            conn.execute(
                """
                UPDATE gateway_approval_requests
                SET status='execution_unknown', updated_at=?
                WHERE status='executing'
                  AND (
                      execution_started=1
                      OR NOT EXISTS (
                          SELECT 1
                          FROM gateway_message_queue AS queue
                          WHERE queue.route_key=gateway_approval_requests.route_key
                            AND queue.task_kind='approval_resume'
                            AND queue.approval_id=gateway_approval_requests.id
                            AND queue.status IN ('queued', 'processing')
                      )
                  )
                """,
                (now,),
            )
        for request in requests:
            _emit_gateway_approval_audit(
                request,
                decision="execution_unknown",
                grant_scope=None,
                decision_source="crash_recovery",
                timestamp=now,
            )
    return {"expired": expired, "execution_unknown": len(requests)}


def create_gateway_approval_with_outbox(
    conn: sqlite3.Connection,
    session_id: str,
    request: dict,
    requester_user_id: str,
    assistant_msg: dict,
    outbox: dict,
    ttl_seconds: float,
    *,
    agent_state: dict | None = None,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> str:
    """原子写入审批请求、审批问题及其 Outbox。"""
    outbox = dict(outbox)
    request_id = str(request.get("id", ""))
    tool_name = request.get("tool_name")
    tool_call_id = str(request.get("tool_call_id", ""))
    tool_args = request.get("arguments")
    details = request.get("details", {})
    request_session_key = str(request.get("session_key", "")).strip()
    request_fingerprint = request.get("fingerprint")
    normalized_requester_user_id = str(requester_user_id or "").strip()
    if not request_id.startswith("approval_"):
        raise DBError("invalid gateway approval request id")
    if not isinstance(tool_name, str) or _TOOL_NAME_RE.fullmatch(tool_name) is None:
        raise DBError("invalid gateway approval tool")
    if not tool_call_id or not isinstance(tool_args, dict):
        raise DBError("invalid gateway approval tool call")
    if not isinstance(details, dict):
        raise DBError("invalid gateway approval details")
    if request_session_key != session_id:
        raise DBError("gateway approval session binding is invalid")
    if request_fingerprint != details.get("fingerprint"):
        raise DBError("gateway approval fingerprint binding is invalid")
    if not approval_request_binding_matches(
        tool_name,
        tool_args,
        details,
        session_key=session_id,
    ):
        raise DBError("invalid gateway approval operation binding")
    if not normalized_requester_user_id:
        raise DBError("gateway approval requester identity is required")
    if not isinstance(assistant_msg, dict) or assistant_msg.get("role") != "assistant":
        raise InvalidMessageError("approval delivery must reference an assistant message")
    source_event_json = str(outbox.get("event_json", "") or "")
    if not source_event_json:
        raise DBError("gateway approval source event is missing")
    source_message_id = str(outbox.get("source_message_id", "") or "")
    source_ids = gateway_event_source_message_ids(
        source_event_json,
        source_message_id,
    )
    if source_message_id not in source_ids:
        raise DBError("gateway approval source event identity mismatch")
    normalized_agent_state = _normalize_gateway_approval_agent_state(agent_state)
    encoded_tool_args = _serialize_gateway_json(tool_args, "approval tool args")
    encoded_details = _serialize_gateway_json(details, "approval details")
    encoded_agent_state = _serialize_gateway_json(
        normalized_agent_state,
        "approval agent state",
    )
    ttl = float(ttl_seconds)
    if not math.isfinite(ttl) or ttl <= 0:
        raise DBError("gateway approval ttl must be positive")

    try:
        created_at = float(request.get("created_at"))
        expires_at = float(request.get("expires_at"))
    except (TypeError, ValueError) as exc:
        raise DBError("gateway approval lifetime binding is invalid") from exc
    if (
        not math.isfinite(created_at)
        or not math.isfinite(expires_at)
        or created_at >= expires_at
        or expires_at <= time.time()
        or abs((expires_at - created_at) - ttl) > 0.001
    ):
        raise DBError("gateway approval lifetime binding is invalid")

    now = time.time()
    with transaction(conn):
        if str(outbox.get("delivery_kind", "")) == "approval_request":
            first_request = conn.execute(
                """
                SELECT id
                FROM gateway_approval_requests
                WHERE route_key=? AND source_message_id=?
                ORDER BY created_at, id
                LIMIT 1
                """,
                (str(outbox["route_key"]), source_message_id),
            ).fetchone()
            if first_request is not None and str(first_request[0]) != request_id:
                outbox["delivery_kind"] = f"approval_request:{request_id}"

        tool_row = conn.execute(
            """
            SELECT id, content
            FROM messages
            WHERE session_id=? AND role='tool' AND tool_call_id=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (session_id, tool_call_id),
        ).fetchone()
        if tool_row is None or request_id not in str(tool_row[1] or ""):
            raise DBError("approval request is not bound to its tool result")
        try:
            placeholder = json.loads(str(tool_row[1]))
            placeholder_request = placeholder["approval_request"]
        except (KeyError, TypeError, ValueError) as exc:
            raise DBError("gateway approval placeholder is invalid") from exc
        if (
            not isinstance(placeholder, dict)
            or not isinstance(placeholder_request, dict)
            or placeholder_request.get("id") != request_id
        ):
            raise DBError("gateway approval placeholder identity mismatch")
        placeholder_request.update({
            "created_at": created_at,
            "expires_at": expires_at,
            "session_key": session_id,
            "tool_call_id": tool_call_id,
            "fingerprint": request_fingerprint,
        })
        conn.execute(
            "UPDATE messages SET content=? WHERE id=?",
            (
                _serialize_gateway_json(
                    placeholder,
                    "approval placeholder",
                ),
                int(tool_row[0]),
            ),
        )

        existing_row = conn.execute(
            f"""
            SELECT {_GATEWAY_APPROVAL_COLUMNS}
            FROM gateway_approval_requests
            WHERE id=?
            """,
            (request_id,),
        ).fetchone()
        if existing_row is not None:
            existing = _gateway_approval_row(existing_row)
            if (
                existing["route_key"] != str(outbox["route_key"])
                or existing["conversation_id"] != session_id
                or existing["source_message_id"] != source_message_id
                or existing["tool_call_id"] != tool_call_id
                or existing["tool_message_id"] != int(tool_row[0])
                or existing["tool_name"] != tool_name
                or existing["tool_args"] != tool_args
                or existing["requester_user_id"] != normalized_requester_user_id
                or existing["source_event_json"] != source_event_json
                or existing["agent_state"] != normalized_agent_state
                or existing["created_at"] != created_at
                or existing["expires_at"] != expires_at
            ):
                raise DBError("gateway approval idempotency identity mismatch")
            outbox_row = conn.execute(
                """
                SELECT id
                FROM gateway_outbox
                WHERE route_key=? AND source_message_id=?
                  AND delivery_kind=?
                """,
                (
                    str(outbox["route_key"]),
                    source_message_id,
                    str(outbox["delivery_kind"]),
                ),
            ).fetchone()
            if outbox_row is None:
                raise DBError("gateway approval is missing its outbox")
            return str(outbox_row[0])

        conn.execute(
            """
            INSERT INTO gateway_approval_requests (
                id, route_key, conversation_id, requester_user_id,
                source_message_id, tool_call_id, tool_message_id, tool_name,
                tool_args_json, summary, details_json, status,
                source_event_json, agent_state_json,
                created_at, expires_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?
            )
            """,
            (
                request_id,
                str(outbox["route_key"]),
                session_id,
                normalized_requester_user_id,
                source_message_id,
                tool_call_id,
                int(tool_row[0]),
                tool_name,
                encoded_tool_args,
                str(request.get("summary", "需要批准的工具操作")),
                encoded_details,
                source_event_json,
                encoded_agent_state,
                created_at,
                expires_at,
                now,
            ),
        )
        assistant_message_id = _insert_message(conn, session_id, assistant_msg)
        outbox_id = _insert_gateway_outbox(
            conn,
            outbox,
            lease_name=lease_name,
            instance_id=instance_id,
            lease_epoch=lease_epoch,
        )
        _insert_gateway_message_delivery(
            conn,
            delivery_id=outbox_id,
            session_id=session_id,
            assistant_message_id=assistant_message_id,
            route_key=str(outbox["route_key"]),
            source_message_id=str(outbox["source_message_id"]),
        )
        _set_gateway_queue_status(
            conn,
            str(outbox["route_key"]),
            str(outbox.get("queue_message_id") or source_message_id),
            "reply_pending",
            now,
        )
    emit_approval_audit(
        request_id=request_id,
        session_key=session_id,
        tool_name=tool_name,
        risk_level=details.get("risk_level"),
        reason=details.get("reason"),
        decision="pending",
        grant_scope=None,
        decision_source=details.get("decision_source", "approval_policy"),
        timestamp=created_at,
    )
    return outbox_id


def fail_gateway_approval_identity_unavailable(
    conn: sqlite3.Connection,
    session_id: str,
    request_id: str,
    tool_call_id: str,
) -> str:
    """在不创建审批记录时，把占位 Tool Result 原子收敛为身份失败。"""
    result_content = _serialize_gateway_json(
        {
            "ok": False,
            "error_type": "approval_identity_unavailable",
            "error": "approval requires a verifiable platform actor identity",
        },
        "approval identity failure",
    )
    with transaction(conn):
        existing = conn.execute(
            "SELECT 1 FROM gateway_approval_requests WHERE id=?",
            (str(request_id),),
        ).fetchone()
        if existing is not None:
            raise DBError("gateway approval identity failure already has a request")
        tool_row = conn.execute(
            """
            SELECT id, content
            FROM messages
            WHERE session_id=? AND role='tool' AND tool_call_id=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (str(session_id), str(tool_call_id)),
        ).fetchone()
        if tool_row is None or str(request_id) not in str(tool_row[1] or ""):
            raise DBError("approval identity failure is not bound to its tool result")
        conn.execute(
            "UPDATE messages SET content=? WHERE id=?",
            (result_content, int(tool_row[0])),
        )
    return result_content


def get_pending_gateway_approval(
    conn: sqlite3.Connection,
    route_key: str,
    conversation_id: str,
) -> dict | None:
    """返回当前对话最早的未决审批；读取前先收敛过期状态。"""
    expire_gateway_approvals(conn)
    row = conn.execute(
        f"""
        SELECT {_GATEWAY_APPROVAL_COLUMNS}
        FROM gateway_approval_requests
        WHERE route_key=? AND conversation_id=? AND status='pending'
        ORDER BY created_at
        LIMIT 1
        """,
        (route_key, conversation_id),
    ).fetchone()
    return _gateway_approval_row(row)


def _select_gateway_approval(
    conn: sqlite3.Connection,
    route_key: str,
    selector: str | None,
    *,
    conversation_id: str | None = None,
) -> tuple[str, dict | None]:
    """选择审批请求；省略 selector 时仅接受当前对话唯一 pending。"""
    normalized = str(selector or "").strip()
    if not normalized:
        normalized_conversation_id = str(conversation_id or "").strip()
        if not normalized_conversation_id:
            return "invalid_id", None
        rows = conn.execute(
            f"""
            SELECT {_GATEWAY_APPROVAL_COLUMNS}
            FROM gateway_approval_requests
            WHERE route_key=? AND conversation_id=? AND status='pending'
            ORDER BY created_at
            LIMIT 2
            """,
            (route_key, normalized_conversation_id),
        ).fetchall()
        if not rows:
            return "not_found", None
        if len(rows) > 1:
            # 正常 Agent 暂停链路只会产生一个 pending。历史异常或并发
            # 状态下宁可拒绝自动选择，也不能让无编号命令批准错误请求。
            return "ambiguous", None
        return "found", _gateway_approval_row(rows[0])
    if any(
        not (char.isalnum() or char in {"_", "-"})
        for char in normalized
    ):
        return "invalid_id", None
    prefix = normalized if normalized.startswith("approval_") else f"approval_{normalized}"
    rows = conn.execute(
        f"""
        SELECT {_GATEWAY_APPROVAL_COLUMNS}
        FROM gateway_approval_requests
        WHERE route_key=? AND substr(id, 1, ?)=?
        ORDER BY created_at DESC
        LIMIT 2
        """,
        (route_key, len(prefix), prefix),
    ).fetchall()
    if not rows:
        return "not_found", None
    if len(rows) > 1:
        return "ambiguous", None
    return "found", _gateway_approval_row(rows[0])


def _claim_gateway_approval_in_transaction(
    conn: sqlite3.Connection,
    route_key: str,
    conversation_id: str,
    requester_user_id: str,
    selector: str | None,
    decision_message_id: str,
    grant_scope: str = "once",
    *,
    now: float,
) -> dict:
    """在调用方事务中验证并领取审批，不创建任何投递或恢复工作。"""
    _expire_gateway_approvals_in_transaction(conn, now)
    outcome, request = _select_gateway_approval(
        conn,
        route_key,
        selector,
        conversation_id=conversation_id,
    )
    if request is None:
        return {"outcome": outcome}
    current_actor_id = str(requester_user_id or "").strip()
    stored_actor_id = str(request["requester_user_id"] or "").strip()
    if (
        not current_actor_id
        or not stored_actor_id
        or stored_actor_id != current_actor_id
    ):
        return {"outcome": "forbidden", "request": request}
    if request["conversation_id"] != conversation_id:
        return {"outcome": "stale_conversation", "request": request}
    if request["status"] != "pending":
        return {"outcome": request["status"], "request": request}
    normalized_scope = str(grant_scope or "").strip().lower()
    if normalized_scope not in {"once", "session"}:
        return {"outcome": "invalid_scope", "request": request}
    if not is_grant_scope_allowed(
        request["details"].get("risk_level"),
        normalized_scope,
    ):
        return {"outcome": "scope_forbidden", "request": request}
    changed = conn.execute(
        """
        UPDATE gateway_approval_requests
        SET status='executing', decision_message_id=?, grant_scope=?,
            execution_started=0, updated_at=?
        WHERE id=? AND status='pending'
        """,
        (decision_message_id, normalized_scope, now, request["id"]),
    ).rowcount
    if changed != 1:
        return {"outcome": "conflict"}
    request["grant_scope"] = normalized_scope
    request["status"] = "executing"
    request["decision_message_id"] = decision_message_id
    request["execution_started"] = False
    request["updated_at"] = now
    return {"outcome": "claimed", "request": request}


def _create_gateway_approval_resume_task_in_transaction(
    conn: sqlite3.Connection,
    request: dict,
) -> dict:
    """为已领取审批写入唯一的原会话恢复任务。"""
    message_id, event_json = _build_gateway_approval_resume_event(request)
    accepted = _enqueue_gateway_message_in_transaction(
        conn,
        request["route_key"],
        message_id,
        event_json,
        task_kind="approval_resume",
        approval_id=request["id"],
    )
    if not accepted:
        raise DBError("gateway approval resume queue identity is occupied")
    resume_task = _gateway_approval_resume_task_row(
        conn,
        request["route_key"],
        message_id,
    )
    if (
        resume_task is None
        or resume_task["task_kind"] != "approval_resume"
        or resume_task["approval_id"] != request["id"]
        or resume_task["event_json"] != event_json
    ):
        raise DBError("gateway approval resume task identity mismatch")
    return resume_task


def _emit_claimed_gateway_approval_audit(request: dict, now: float) -> None:
    """仅在领取、投递确认和恢复任务均已提交后记录审批审计。"""
    _emit_gateway_approval_audit(
        request,
        decision="approved",
        grant_scope=request["grant_scope"],
        decision_source="user",
        timestamp=now,
    )


def claim_gateway_approval(
    conn: sqlite3.Connection,
    route_key: str,
    conversation_id: str,
    requester_user_id: str,
    selector: str | None,
    decision_message_id: str,
    grant_scope: str = "once",
) -> dict:
    """领取审批并写入恢复任务，保留没有确认回执的嵌入式兼容入口。"""
    now = time.time()
    with transaction(conn):
        decision = _claim_gateway_approval_in_transaction(
            conn,
            route_key,
            conversation_id,
            requester_user_id,
            selector,
            decision_message_id,
            grant_scope,
            now=now,
        )
        if decision["outcome"] != "claimed":
            return decision
        decision["resume_task"] = _create_gateway_approval_resume_task_in_transaction(
            conn,
            decision["request"],
        )
    _emit_claimed_gateway_approval_audit(decision["request"], now)
    return decision


def claim_gateway_approval_with_ack_outbox(
    conn: sqlite3.Connection,
    route_key: str,
    conversation_id: str,
    requester_user_id: str,
    selector: str | None,
    decision_message_id: str,
    grant_scope: str,
    ack_outbox: dict,
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> dict:
    """原子领取审批、保存确认 Outbox，并写入原会话恢复任务。"""
    now = time.time()
    with transaction(conn):
        decision = _claim_gateway_approval_in_transaction(
            conn,
            route_key,
            conversation_id,
            requester_user_id,
            selector,
            decision_message_id,
            grant_scope,
            now=now,
        )
        if decision["outcome"] != "claimed":
            return decision
        if (
            not isinstance(ack_outbox, dict)
            or str(ack_outbox.get("route_key", "")) != route_key
            or str(ack_outbox.get("source_message_id", ""))
            != decision_message_id
            or str(ack_outbox.get("queue_message_id", decision_message_id))
            != decision_message_id
        ):
            raise DBError("gateway approval acknowledgement outbox is invalid")
        decision["ack_outbox_id"] = _insert_gateway_outbox(
            conn,
            ack_outbox,
            lease_name=lease_name,
            instance_id=instance_id,
            lease_epoch=lease_epoch,
        )
        decision["resume_task"] = _create_gateway_approval_resume_task_in_transaction(
            conn,
            decision["request"],
        )
    _emit_claimed_gateway_approval_audit(decision["request"], now)
    return decision


def deny_gateway_approval(
    conn: sqlite3.Connection,
    route_key: str,
    conversation_id: str,
    requester_user_id: str,
    selector: str | None,
    decision_message_id: str,
) -> dict:
    """校验审批归属并把当前唯一 pending 原子转为 denied。"""
    now = time.time()
    with transaction(conn):
        _expire_gateway_approvals_in_transaction(conn, now)
        outcome, request = _select_gateway_approval(
            conn,
            route_key,
            selector,
            conversation_id=conversation_id,
        )
        if request is None:
            return {"outcome": outcome}
        current_actor_id = str(requester_user_id or "").strip()
        stored_actor_id = str(request["requester_user_id"] or "").strip()
        if (
            not current_actor_id
            or not stored_actor_id
            or stored_actor_id != current_actor_id
        ):
            return {"outcome": "forbidden", "request": request}
        if request["conversation_id"] != conversation_id:
            return {"outcome": "stale_conversation", "request": request}
        if request["status"] != "pending":
            return {"outcome": request["status"], "request": request}
        changed = conn.execute(
            """
            UPDATE gateway_approval_requests
            SET status='denied', decision_message_id=?, updated_at=?
            WHERE id=? AND status='pending'
            """,
            (decision_message_id, now, request["id"]),
        ).rowcount
        if changed != 1:
            return {"outcome": "conflict"}
        conn.execute(
            "UPDATE messages SET content=? WHERE id=?",
            (
                _approval_terminal_content(request["id"], "denied"),
                request["tool_message_id"],
            ),
        )
        request["status"] = "denied"
        _emit_gateway_approval_audit(
            request,
            decision="denied",
            grant_scope=None,
            decision_source="user",
            timestamp=now,
        )
        return {"outcome": "denied", "request": request}


def finish_gateway_approval(
    conn: sqlite3.Connection,
    request_id: str,
    result_content: str,
    *,
    succeeded: bool,
) -> dict:
    """保存一次性执行结果，并用真实结果替换 awaiting Tool Result。"""
    final_status = "executed" if succeeded else "failed"
    now = time.time()
    with transaction(conn):
        row = conn.execute(
            f"""
            SELECT {_GATEWAY_APPROVAL_COLUMNS}
            FROM gateway_approval_requests
            WHERE id=?
            """,
            (request_id,),
        ).fetchone()
        request = _gateway_approval_row(row)
        if request is None:
            raise DBError("gateway approval request not found")
        changed = conn.execute(
            """
            UPDATE gateway_approval_requests
            SET status=?, result_content=?, updated_at=?
            WHERE id=? AND status='executing' AND execution_started=1
            """,
            (final_status, str(result_content), now, request_id),
        ).rowcount
        if changed != 1:
            raise DBError("gateway approval is not executing")
        conn.execute(
            "UPDATE messages SET content=? WHERE id=?",
            (str(result_content), request["tool_message_id"]),
        )
        request["status"] = final_status
        request["result_content"] = str(result_content)
        _emit_gateway_approval_audit(
            request,
            decision=final_status,
            grant_scope=None,
            decision_source="tool_execution",
            timestamp=now,
        )
        return request


def _approval_resume_message_id(request_id: str) -> str:
    normalized = str(request_id or "")
    if not normalized.startswith("approval_"):
        raise DBError("invalid gateway approval request id")
    return f"approval-resume:{normalized}"


def _build_gateway_approval_resume_event(
    request: dict,
) -> tuple[str, str]:
    """从审批保存的原始事件构造最小内部恢复事件。"""
    source_event_json = request.get("source_event_json")
    if not isinstance(source_event_json, str) or not source_event_json:
        raise DBError("gateway approval source event is unavailable")
    try:
        source_payload = json.loads(source_event_json)
    except (TypeError, ValueError) as exc:
        raise DBError("gateway approval source event is invalid") from exc
    if not isinstance(source_payload, dict):
        raise DBError("gateway approval source event must contain an object")
    source = source_payload.get("source")
    if not isinstance(source, dict):
        raise DBError("gateway approval source identity is invalid")
    if str(source_payload.get("message_id", "")) != request["source_message_id"]:
        raise DBError("gateway approval source message identity mismatch")

    message_id = _approval_resume_message_id(request["id"])
    event = {
        "message_id": message_id,
        "text": "",
        "message_type": "text",
        "media_urls": [],
        "reply_to_message_id": None,
        "attachments": [],
        "metadata": {
            "gateway_internal_task": "approval_resume",
            "approval_id": request["id"],
        },
        "source": dict(source),
    }
    return message_id, _serialize_gateway_json(event, "approval resume event")


def _gateway_approval_resume_task_row(
    conn: sqlite3.Connection,
    route_key: str,
    message_id: str,
) -> dict | None:
    row = conn.execute(
        """
        SELECT event_json, status, task_kind, approval_id
        FROM gateway_message_queue
        WHERE route_key=? AND message_id=?
        """,
        (route_key, message_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "route_key": route_key,
        "message_id": message_id,
        "event_json": str(row[0]),
        "status": str(row[1]),
        "task_kind": str(row[2]),
        "approval_id": str(row[3]) if row[3] is not None else None,
    }


def _approval_history_matches(
    conn: sqlite3.Connection,
    approval: dict,
) -> bool:
    """确认恢复只会执行数据库历史中那一次已审批的原始调用。"""
    rows = conn.execute(
        """
        SELECT id, role, content, tool_calls, tool_call_id
        FROM messages
        WHERE session_id=?
        ORDER BY id
        """,
        (approval["conversation_id"],),
    ).fetchall()
    matching_calls = 0
    matching_results: list[str] = []
    for message_id, role, content, tool_calls_json, tool_call_id in rows:
        if role == "assistant" and tool_calls_json:
            try:
                tool_calls = json.loads(tool_calls_json)
            except (TypeError, ValueError):
                return False
            if not isinstance(tool_calls, list):
                return False
            for call in tool_calls:
                if not isinstance(call, dict) or call.get("id") != approval["tool_call_id"]:
                    continue
                function = call.get("function")
                if not isinstance(function, dict):
                    return False
                try:
                    arguments = json.loads(function.get("arguments"))
                except (TypeError, ValueError):
                    return False
                if (
                    function.get("name") != approval["tool_name"]
                    or arguments != approval["tool_args"]
                ):
                    return False
                matching_calls += 1
        if role == "tool" and tool_call_id == approval["tool_call_id"]:
            if int(message_id) != approval["tool_message_id"]:
                return False
            matching_results.append(str(content or ""))
    if matching_calls != 1 or len(matching_results) != 1:
        return False
    current_result = matching_results[0]
    if approval["status"] == "executing":
        return _is_approval_placeholder(current_result, approval["id"])
    return (
        approval["status"] in {"executed", "failed"}
        and approval["result_content"] is not None
        and current_result == approval["result_content"]
    )


def _is_approval_placeholder(content: object, approval_id: str) -> bool:
    """识别尚未被真实工具结果替换的审批占位结果。"""
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return False
    request = payload.get("approval_request") if isinstance(payload, dict) else None
    return (
        isinstance(payload, dict)
        and payload.get("approval_required") is True
        and isinstance(request, dict)
        and request.get("id") == approval_id
    )


def begin_gateway_approval_execution(
    conn: sqlite3.Connection,
    route_key: str,
    conversation_id: str,
    message_id: str,
    approval_id: str,
) -> dict | None:
    """在 route worker 即将派发工具前把可安全恢复的审批标记为已开始。"""
    with transaction(conn):
        resume = get_gateway_approval_resume(
            conn,
            route_key,
            conversation_id,
            message_id,
            approval_id,
        )
        if resume is None:
            return None
        approval = resume["approval"]
        if approval["status"] != "executing" or approval["execution_started"]:
            return None
        changed = conn.execute(
            """
            UPDATE gateway_approval_requests
            SET execution_started=1, updated_at=?
            WHERE id=? AND status='executing' AND execution_started=0
            """,
            (time.time(), approval_id),
        ).rowcount
        if changed != 1:
            return None
        approval["execution_started"] = True
        return approval


def get_gateway_approval_resume(
    conn: sqlite3.Connection,
    route_key: str,
    conversation_id: str,
    message_id: str,
    approval_id: str,
) -> dict | None:
    """验证数据库内部恢复任务与审批终态的完整绑定。"""
    expected_message_id = _approval_resume_message_id(approval_id)
    if str(message_id) != expected_message_id:
        return None
    task = _gateway_approval_resume_task_row(
        conn,
        str(route_key),
        expected_message_id,
    )
    if (
        task is None
        or task["status"] not in {"queued", "processing"}
        or task["task_kind"] != "approval_resume"
        or task["approval_id"] != approval_id
    ):
        return None

    row = conn.execute(
        f"""
        SELECT {_GATEWAY_APPROVAL_COLUMNS}
        FROM gateway_approval_requests
        WHERE id=? AND route_key=? AND conversation_id=?
          AND status IN ('executing', 'executed', 'failed')
        """,
        (approval_id, route_key, conversation_id),
    ).fetchone()
    approval = _gateway_approval_row(row)
    if approval is None:
        return None
    if (
        not approval["fingerprint"]
        or not approval_request_binding_matches(
            approval["tool_name"],
            approval["tool_args"],
            approval["details"],
            session_key=conversation_id,
        )
    ):
        return None
    expected_id, expected_event_json = _build_gateway_approval_resume_event(approval)
    if expected_id != message_id or task["event_json"] != expected_event_json:
        return None

    source_event_json = approval.get("source_event_json")
    source_ids = gateway_event_source_message_ids(
        str(source_event_json),
        approval["source_message_id"],
    )
    if approval["source_message_id"] not in source_ids:
        return None
    if not _approval_history_matches(conn, approval):
        return None
    return {
        "approval": approval,
        "resume_task": task,
    }


def cancel_pending_gateway_approvals(
    conn: sqlite3.Connection,
    route_key: str,
    conversation_id: str,
    *,
    decision_source: str = "conversation_lifecycle",
) -> int:
    """取消当前对话的全部 pending 请求，不触碰已经开始执行的请求。"""
    return cancel_pending_gateway_approvals_for_session(
        conn,
        conversation_id,
        route_key=route_key,
        decision_source=decision_source,
    )


def cancel_pending_gateway_approvals_for_session(
    conn: sqlite3.Connection,
    conversation_id: str,
    *,
    route_key: str | None = None,
    decision_source: str = "session_cleanup",
) -> int:
    """按真实 session 收敛 pending，可选 route 仅用于进一步限定。"""
    now = time.time()
    where = "conversation_id=? AND status='pending'"
    params: list[object] = [conversation_id]
    if route_key is not None:
        where = "route_key=? AND " + where
        params.insert(0, route_key)
    with transaction(conn):
        rows = conn.execute(
            f"""
            SELECT {_GATEWAY_APPROVAL_COLUMNS}
            FROM gateway_approval_requests
            WHERE {where}
            """,
            tuple(params),
        ).fetchall()
        requests = [_gateway_approval_row(row) for row in rows]
        for request in requests:
            conn.execute(
                "UPDATE messages SET content=? WHERE id=?",
                (
                    _approval_terminal_content(request["id"], "cancelled"),
                    request["tool_message_id"],
                ),
            )
        if requests:
            conn.execute(
                f"""
                UPDATE gateway_approval_requests
                SET status='cancelled', updated_at=?
                WHERE {where}
                """,
                (now, *params),
            )
        for request in requests:
            _emit_gateway_approval_audit(
                request,
                decision="cancelled",
                grant_scope=None,
                decision_source=decision_source,
                timestamp=now,
            )
    return len(requests)

