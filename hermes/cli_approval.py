"""CLI 交互式审批的受信任签发和单次执行边界。"""

from __future__ import annotations

import sqlite3

from hermes.approval_policy import (
    activate_session_grant,
    issue_interactive_approval_grant,
)
from hermes.db import replace_tool_message_content
from hermes.durable_tool_dispatcher import tool_output_failed
from hermes.tools import ApprovalMode, registry


def execute_cli_approval(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    request: dict,
    scope: str,
) -> str:
    """只把当前 CLI 循环保存的待审批调用交给原工具 handler。"""
    grant = issue_interactive_approval_grant(
        request,
        session_key=session_id,
        scope=scope,
    )
    entry = registry.get_entry(grant.tool_name)
    if entry is None or entry.approval_mode == ApprovalMode.NONE:
        raise ValueError("interactive approval tool is unavailable")
    output = registry.dispatch(
        grant.tool_name,
        grant.arguments,
        session_key=grant.session_key,
        interactive_approval=True,
        approval_grant=grant,
    )
    if not replace_tool_message_content(
        conn,
        session_id,
        str(request.get("tool_call_id", "")),
        output,
    ):
        raise RuntimeError("interactive approval tool result is unavailable")
    if not tool_output_failed(output) and grant.scope == "session":
        activate_session_grant(grant)
    return output
