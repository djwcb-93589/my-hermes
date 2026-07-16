"""File / Terminal 远程审批的共享协议与保守判定。"""

from __future__ import annotations

import json
import re
import shlex
import uuid


APPROVAL_REQUIRED_ERROR = "approval_required"
REMOTE_APPROVAL_MODE = "remote"

# 只把严格的 cwd 查询/切换视为无需审批。任何 shell 控制符、变量展开、
# 命令替换或复合命令都会退回审批路径，避免把任意执行伪装成 cd。
_UNSAFE_CWD_COMMAND_CHARS_RE = re.compile(r"[;&|<>`$\r\n(){}]")


def is_remote_approval(kwargs: dict) -> bool:
    """调用是否来自需要远程审批的工具会话。"""
    return kwargs.get("approval_mode") == REMOTE_APPROVAL_MODE


def has_approval_grant(
    kwargs: dict,
    tool_name: str,
    arguments: dict,
) -> bool:
    """一次性许可必须同时绑定请求 ID、工具名和完整原始参数。"""
    grant = kwargs.get("approval_grant")
    if not isinstance(grant, dict):
        return False
    request_id = grant.get("id")
    return (
        isinstance(request_id, str)
        and request_id.startswith("approval_")
        and grant.get("tool_name") == tool_name
        and grant.get("arguments") == arguments
    )


def build_approval_required(
    tool_name: str,
    summary: str,
    *,
    details: dict | None = None,
) -> str:
    """构造不会携带完整写入内容的待审批 Tool Result。"""
    request_id = f"approval_{uuid.uuid4().hex}"
    return json.dumps(
        {
            "ok": False,
            "status": "awaiting_approval",
            "error_type": APPROVAL_REQUIRED_ERROR,
            "approval_required": True,
            "approval_request": {
                "id": request_id,
                "tool_name": tool_name,
                "summary": summary,
                "details": dict(details or {}),
            },
        },
        ensure_ascii=False,
    )


def build_approval_deferred() -> str:
    """同一模型响应出现多个工具调用时，只保留第一个审批请求。"""
    return json.dumps(
        {
            "ok": False,
            "error_type": "approval_deferred",
            "error": (
                "Tool call was not executed because another operation "
                "is already awaiting approval."
            ),
        },
        ensure_ascii=False,
    )


def is_cwd_only_terminal_command(command: object) -> bool:
    """只认可单条纯 ``cd`` / ``pwd``，其余 Terminal 命令全部审批。"""
    if not isinstance(command, str):
        return False
    stripped = command.strip()
    if not stripped or _UNSAFE_CWD_COMMAND_CHARS_RE.search(stripped):
        return False
    try:
        tokens = shlex.split(stripped, posix=True)
    except ValueError:
        return False
    if tokens == ["pwd"]:
        return True
    if not tokens or tokens[0] != "cd":
        return False
    if len(tokens) <= 2:
        return True
    return len(tokens) == 3 and tokens[1] == "--"
