"""Gateway 出站文件工具：审批后只创建持久 pending 任务。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from hermes.approval import build_assessment_response, is_remote_approval
from hermes.approval_policy import assess_gateway_send_file
from hermes.approval_handlers import get_approval_handler, register_approval_handler
from hermes.tools.gateway_send_file_approval import GatewaySendFileApprovalHandler
from hermes.db import DBError
from hermes.gateway.outbound_delivery import OutboundDeliveryService
from hermes.outbound_file import (
    OutboundFileValidationError,
)


_INTERNAL_ARGUMENT_FIELDS = frozenset({
    "approval_grant",
    "gateway_context",
    "route_key",
    "conversation_id",
    "platform",
    "chat_id",
})


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _error(error_type: str, error: str, *, fatal: bool = True) -> str:
    return _json({
        "ok": False,
        "error_type": error_type,
        "error": error,
        "fatal": fatal,
    })


def _gateway_execution_context(kwargs: dict) -> dict | None:
    """只接受 Runner 通过 keyword-only 参数注入的可信平台身份。"""
    if kwargs.get("gateway_context") is not True:
        return None
    context = {
        "route_key": kwargs.get("gateway_route_key"),
        "conversation_id": kwargs.get("gateway_conversation_id"),
        "source_message_id": kwargs.get("gateway_source_message_id"),
        "platform": kwargs.get("gateway_platform"),
        "chat_id": kwargs.get("gateway_chat_id"),
        "reply_to_message_id": kwargs.get("gateway_reply_to_message_id"),
        "thread_id": kwargs.get("gateway_thread_id"),
        "db_path": kwargs.get("gateway_db_path"),
        "file_transfer_config": kwargs.get("gateway_file_transfer_config"),
        "runtime_fence": kwargs.get("gateway_runtime_fence"),
    }
    for field_name in (
        "route_key",
        "conversation_id",
        "source_message_id",
        "platform",
        "chat_id",
        "db_path",
    ):
        value = context[field_name]
        if not isinstance(value, str) or not value:
            return None
    if kwargs.get("session_key") != context["conversation_id"]:
        return None
    if not isinstance(context["file_transfer_config"], Mapping):
        return None
    fence = context["runtime_fence"]
    if not isinstance(fence, Mapping) or not all(
        fence.get(field_name) is not None
        for field_name in ("lease_name", "instance_id", "lease_epoch")
    ):
        return None
    return context


def handle_gateway_send_file(args: dict, **kwargs) -> str:
    """校验文件、走远程审批，批准后幂等创建 pending delivery。"""
    if not isinstance(args, dict):
        return _error("invalid_args", "arguments must be an object")
    if any(field_name in args for field_name in _INTERNAL_ARGUMENT_FIELDS):
        return _error("invalid_args", "unexpected internal-only argument")
    unknown_fields = set(args) - {"path", "display_name"}
    if unknown_fields:
        return _error("invalid_args", "unexpected gateway_send_file argument")

    context = _gateway_execution_context(kwargs)
    if context is None:
        return _error(
            "forbidden",
            "gateway_send_file requires a current Gateway route and platform context",
        )
    if not is_remote_approval(kwargs):
        return _error(
            "forbidden",
            "gateway_send_file requires the remote approval workflow",
        )

    file_config = context["file_transfer_config"]
    if file_config.get("enabled") is not True:
        return _error(
            "file_transfer_disabled",
            "gateway.file_transfer.enabled must be true",
        )
    path = args.get("path")
    try:
        delivery_service = OutboundDeliveryService(
            context["db_path"],
            file_config,
        )
        snapshot = delivery_service.capture_file(path, args.get("display_name"))
        display_name = snapshot["display_name"]
    except OutboundFileValidationError as exc:
        return _error(exc.error_code, str(exc))

    assessment = assess_gateway_send_file(
        args,
        file_snapshot=snapshot,
        session_key=context["conversation_id"],
        route_key=context["route_key"],
        source_message_id=context["source_message_id"],
        platform=context["platform"],
        chat_id=context["chat_id"],
        reply_to_message_id=context["reply_to_message_id"],
        thread_id=context["thread_id"],
        remote_approval=True,
        approval_grant=kwargs.get("approval_grant"),
    )
    policy_response = build_assessment_response(
        assessment,
        "向当前 Gateway 会话发送本地文件",
        approval_details={"display_name": display_name},
        denial_payload={"path": path},
    )
    if policy_response is not None:
        return policy_response

    approval_grant = kwargs.get("approval_grant")
    approval_id = str(getattr(approval_grant, "request_id", ""))
    if not approval_id.startswith("approval_"):
        return _error(
            "forbidden",
            "gateway_send_file requires a trusted once approval grant",
        )
    delivery_digest = hashlib.sha256(approval_id.encode("utf-8")).hexdigest()
    delivery_id = f"delivery_{delivery_digest[:32]}"
    delivery = {
        "id": delivery_id,
        "approval_id": approval_id,
        "route_key": context["route_key"],
        "conversation_id": context["conversation_id"],
        "source_message_id": context["source_message_id"],
        "platform": context["platform"],
        "chat_id": context["chat_id"],
        "reply_to_message_id": context["reply_to_message_id"],
        "thread_id": context["thread_id"],
        "local_path": snapshot["abs_path"],
        "display_name": display_name,
        "size_bytes": snapshot["size_bytes"],
        "sha256": snapshot["sha256"],
    }
    try:
        created = delivery_service.create_gateway_file_delivery(
            delivery,
            runtime_fence=context["runtime_fence"],
        )
    except DBError:
        return _error(
            "delivery_persistence_failed",
            "pending file delivery could not be created",
            fatal=False,
        )
    return _json({
        "ok": True,
        "delivery_id": created["id"],
        "status": created["status"],
        "display_name": created["display_name"],
        "size_bytes": created["size_bytes"],
        "sha256": created["sha256"],
    })


def register(registry) -> None:
    if get_approval_handler("gateway_send_file") is None:
        register_approval_handler(
            "gateway_send_file", GatewaySendFileApprovalHandler()
        )
    registry.register(
        name="gateway_send_file",
        toolset="messaging",
        schema={
            "name": "gateway_send_file",
            "description": (
                "Create an approved persistent task to send one local file "
                "to the current Gateway conversation. This tool is only "
                "available in Gateway sessions. Every call requires a once "
                "approval, and approval binds the file path, size, SHA-256, "
                "stable file state, and current platform target. The current "
                "stage creates a pending delivery only; it does not upload or "
                "send the file. Paths must pass the shared filesystem policy "
                "and gateway.file_transfer.outbound_allowed_roots."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Local path of the regular file to send.",
                    },
                    "display_name": {
                        "type": "string",
                        "description": (
                            "Optional plain file name shown to the recipient."
                        ),
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
        handler=handle_gateway_send_file,
        execution_environments=("gateway",),
        unattended_allowed=False,
        required_trusted_context=("gateway_file_delivery",),
        approval_mode="remote_once",
        risk_level="high",
        default_enabled_environments=(),
    )
