"""File / Terminal 远程审批 Tool Result 的共享协议。"""

from __future__ import annotations

import json
import uuid

from hermes.approval_policy import (
    ALLOW,
    ASK,
    ApprovalAssessment,
    emit_approval_audit,
)


APPROVAL_REQUIRED_ERROR = "approval_required"
REMOTE_APPROVAL_MODE = "remote"

def is_remote_approval(kwargs: dict) -> bool:
    """调用是否来自需要远程审批的工具会话。"""
    return kwargs.get("approval_mode") == REMOTE_APPROVAL_MODE


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


def build_assessment_response(
    assessment: ApprovalAssessment,
    summary: str,
    *,
    approval_details: dict | None = None,
    denial_payload: dict | None = None,
) -> str | None:
    """把统一策略结论转换为 Tool Result；ALLOW 不产生响应。"""
    if assessment.decision == ALLOW:
        return None
    if assessment.decision == ASK:
        details = dict(approval_details or {})
        # 策略身份字段拥有最终解释权，调用方不能覆盖指纹或规范化目标。
        details.update(assessment.details)
        return build_approval_required(
            assessment.tool_name,
            summary,
            details=details,
        )

    payload = dict(denial_payload or {})
    payload.update({
        "ok": False,
        "error_type": assessment.error_type or "approval_denied",
        "error": assessment.error or "operation denied by approval policy",
        "fatal": bool(assessment.fatal),
        "risk_level": assessment.risk_level.value,
    })
    emit_approval_audit(
        request_id=None,
        session_key=assessment.session_key,
        tool_name=assessment.tool_name,
        risk_level=assessment.risk_level.value,
        reason=assessment.reason,
        decision="denied",
        grant_scope=None,
        decision_source=assessment.details.get(
            "decision_source",
            "approval_policy",
        ),
    )
    return json.dumps(payload, ensure_ascii=False)
