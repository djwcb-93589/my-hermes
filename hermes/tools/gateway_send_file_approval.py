"""Gateway Send File 的审批决策、Binding 与执行前复检。"""

from __future__ import annotations

import re
from collections.abc import Mapping

from hermes.approval_policy import (
    ALLOW,
    ASK,
    DENY,
    HIGH,
    ApprovalAssessment,
    _approval_details,
    _backend_fingerprint_payload,
    _identifier_fingerprint,
    approval_binding_fingerprint,
    approval_grant_identity_matches,
    normalize_approval_session_key,
)


_TOOL_NAME = "gateway_send_file"
_IDENTITY_FIELDS = (
    "session_key_fingerprint",
    "route_key_fingerprint",
    "source_message_fingerprint",
    "chat_id_fingerprint",
    "reply_to_message_fingerprint",
    "thread_id_fingerprint",
)


def normalize_gateway_send_file_snapshot(value: object) -> dict:
    """校验审批中的出站文件快照，不读取文件正文。"""
    if not isinstance(value, Mapping):
        raise ValueError("gateway send file snapshot must be an object")
    abs_path = value.get("abs_path")
    sha256 = value.get("sha256")
    if not isinstance(abs_path, str) or not abs_path:
        raise ValueError("gateway send file snapshot path is invalid")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("gateway send file snapshot sha256 is invalid")
    normalized = {"abs_path": abs_path, "sha256": sha256}
    for field_name in (
        "size_bytes",
        "device",
        "inode",
        "mtime_ns",
        "ctime_ns",
    ):
        raw = value.get(field_name)
        if isinstance(raw, bool):
            raise ValueError(
                f"gateway send file snapshot {field_name} is invalid"
            )
        try:
            parsed = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"gateway send file snapshot {field_name} is invalid"
            ) from exc
        if parsed < 0 or (field_name == "size_bytes" and parsed <= 0):
            raise ValueError(
                f"gateway send file snapshot {field_name} is invalid"
            )
        normalized[field_name] = parsed
    return normalized


def gateway_send_file_identity_details(
    *,
    session_key: str,
    route_key: str,
    source_message_id: str,
    platform: str,
    chat_id: str,
    reply_to_message_id: str | None,
    thread_id: str | None,
) -> dict:
    """把平台目标收敛为不可逆摘要，并绑定当前 Gateway 会话。"""
    values = {
        "session_key": session_key,
        "route_key": route_key,
        "source_message": source_message_id,
        "platform": platform,
        "chat_id": chat_id,
    }
    if any(
        not isinstance(value, str) or not value.strip()
        for value in values.values()
    ):
        raise ValueError("gateway send file identity is incomplete")
    return {
        "session_key_fingerprint": _identifier_fingerprint(session_key),
        "route_key_fingerprint": _identifier_fingerprint(route_key),
        "source_message_fingerprint": _identifier_fingerprint(
            source_message_id
        ),
        "platform": platform.strip().lower(),
        "chat_id_fingerprint": _identifier_fingerprint(chat_id),
        "reply_to_message_fingerprint": (
            _identifier_fingerprint(reply_to_message_id)
            if isinstance(reply_to_message_id, str) and reply_to_message_id
            else None
        ),
        "thread_id_fingerprint": (
            _identifier_fingerprint(thread_id)
            if isinstance(thread_id, str) and thread_id
            else None
        ),
    }


def _gateway_send_file_binding(
    arguments: dict,
    *,
    file_snapshot: dict,
    session_key: str,
    route_key: str,
    source_message_id: str,
    platform: str,
    chat_id: str,
    reply_to_message_id: str | None,
    thread_id: str | None,
) -> dict:
    snapshot = normalize_gateway_send_file_snapshot(file_snapshot)
    identity = gateway_send_file_identity_details(
        session_key=session_key,
        route_key=route_key,
        source_message_id=source_message_id,
        platform=platform,
        chat_id=chat_id,
        reply_to_message_id=reply_to_message_id,
        thread_id=thread_id,
    )
    return {
        "requested_path": arguments.get("path"),
        "file_snapshot": snapshot,
        "target_identity": {
            field_name: identity.get(field_name)
            for field_name in _IDENTITY_FIELDS
        },
        "platform": identity["platform"],
        "backend_risk": _backend_fingerprint_payload({
            "backend_type": "gateway",
        }),
        "risk_level": HIGH.value,
    }


def _valid_gateway_send_file_binding(
    arguments: dict,
    binding: dict,
    session_key: str,
) -> bool:
    """严格校验文件快照、公开参数与目标会话摘要。"""
    try:
        normalized_session = normalize_approval_session_key(session_key)
        if (
            not isinstance(arguments, dict)
            or set(binding) != {
                "requested_path",
                "file_snapshot",
                "target_identity",
                "platform",
                "backend_risk",
                "risk_level",
            }
            or binding.get("requested_path") != arguments.get("path")
            or not isinstance(binding.get("platform"), str)
            or not binding["platform"]
            or binding.get("risk_level") != HIGH.value
            or _backend_fingerprint_payload(binding.get("backend_risk"))
            != binding.get("backend_risk")
        ):
            return False
        normalize_gateway_send_file_snapshot(binding["file_snapshot"])
        target = binding["target_identity"]
        if not isinstance(target, dict) or set(target) != set(_IDENTITY_FIELDS):
            return False
        if target["session_key_fingerprint"] != _identifier_fingerprint(
            normalized_session
        ):
            return False
        for field_name in _IDENTITY_FIELDS:
            value = target[field_name]
            if value is not None and (
                not isinstance(value, str)
                or re.fullmatch(r"sha256:[0-9a-f]{16}", value) is None
            ):
                return False
        return all(
            isinstance(target[field_name], str)
            for field_name in (
                "session_key_fingerprint",
                "route_key_fingerprint",
                "source_message_fingerprint",
                "chat_id_fingerprint",
            )
        )
    except (KeyError, TypeError, ValueError):
        return False


class GatewaySendFileApprovalHandler:
    """解释 Gateway Send File 一次性审批 Binding。"""

    def validate_request_binding(
        self, *, arguments: dict, binding: dict, session_key: str
    ) -> bool:
        return _valid_gateway_send_file_binding(
            arguments, binding, session_key
        )

    def validate_grant_binding(
        self, *, arguments: dict, binding: dict, session_key: str
    ) -> bool:
        return _valid_gateway_send_file_binding(
            arguments, binding, session_key
        )

    def build_session_rule(self, grant: object) -> None:
        return None

    def session_rule_matches(self, rule: object, runtime_context: object) -> bool:
        return False


_GATEWAY_SEND_FILE_APPROVAL_HANDLER = GatewaySendFileApprovalHandler()


def register_gateway_send_file_approval_handler() -> None:
    """随工具注册唯一的出站文件审批 Handler。"""
    from hermes.approval_handlers import (
        get_approval_handler,
        register_approval_handler,
    )

    registered = get_approval_handler(_TOOL_NAME)
    if registered is None:
        register_approval_handler(
            _TOOL_NAME, _GATEWAY_SEND_FILE_APPROVAL_HANDLER
        )
    elif registered is not _GATEWAY_SEND_FILE_APPROVAL_HANDLER:
        raise ValueError(
            f"approval handler already registered: {_TOOL_NAME}"
        )


def gateway_send_file_grant_matches_runtime(
    approval_grant: object,
    arguments: dict,
    *,
    file_snapshot: dict,
    session_key: str,
    route_key: str,
    source_message_id: str,
    platform: str,
    chat_id: str,
    reply_to_message_id: str | None,
    thread_id: str | None,
) -> bool:
    """在创建 pending delivery 前重建完整目标和文件身份。"""
    try:
        normalized_session = normalize_approval_session_key(session_key)
        binding = _gateway_send_file_binding(
            arguments,
            file_snapshot=file_snapshot,
            session_key=normalized_session,
            route_key=route_key,
            source_message_id=source_message_id,
            platform=platform,
            chat_id=chat_id,
            reply_to_message_id=reply_to_message_id,
            thread_id=thread_id,
        )
        fingerprint = approval_binding_fingerprint(
            _TOOL_NAME,
            arguments,
            session_key=normalized_session,
            binding=binding,
        )
        return bool(
            approval_grant_identity_matches(
                approval_grant, _TOOL_NAME, arguments
            )
            and getattr(approval_grant, "scope", None) == "once"
            and getattr(approval_grant, "session_key", None)
            == normalized_session
            and getattr(approval_grant, "fingerprint", None) == fingerprint
            and getattr(approval_grant, "binding", None) == binding
            and _valid_gateway_send_file_binding(
                arguments, binding, normalized_session
            )
        )
    except (TypeError, ValueError):
        return False


def assess_gateway_send_file(
    arguments: dict,
    *,
    file_snapshot: dict,
    session_key: str,
    route_key: str,
    source_message_id: str,
    platform: str,
    chat_id: str,
    reply_to_message_id: str | None,
    thread_id: str | None,
    remote_approval: bool,
    approval_grant: object = None,
) -> ApprovalAssessment:
    """绑定文件状态和目标会话，并只接受一次性审批。"""
    normalized_arguments = dict(arguments)
    normalized_session = normalize_approval_session_key(session_key)
    binding = _gateway_send_file_binding(
        normalized_arguments,
        file_snapshot=file_snapshot,
        session_key=normalized_session,
        route_key=route_key,
        source_message_id=source_message_id,
        platform=platform,
        chat_id=chat_id,
        reply_to_message_id=reply_to_message_id,
        thread_id=thread_id,
    )
    grant_matches = gateway_send_file_grant_matches_runtime(
        approval_grant,
        normalized_arguments,
        file_snapshot=file_snapshot,
        session_key=normalized_session,
        route_key=route_key,
        source_message_id=source_message_id,
        platform=platform,
        chat_id=chat_id,
        reply_to_message_id=reply_to_message_id,
        thread_id=thread_id,
    )
    if grant_matches:
        decision = ALLOW
        reason = "一次性审批与当前文件快照和目标会话完全一致"
        error_type = error = None
        fatal = False
        decision_source = "once_grant"
    elif approval_grant is not None:
        decision = DENY
        reason = "出站文件审批已过期或文件、目标身份发生变化"
        error_type = "approval_stale"
        error = "approved file or Gateway target changed; request approval again"
        fatal = False
        decision_source = "grant_validation"
    elif remote_approval:
        decision = ASK
        reason = "向平台会话发送本地文件属于受控副作用"
        error_type = error = None
        fatal = False
        decision_source = "approval_policy"
    else:
        decision = DENY
        reason = "出站文件只允许通过 Gateway 远程审批链执行"
        error_type = "forbidden"
        error = "gateway_send_file requires a Gateway remote approval context"
        fatal = True
        decision_source = "gateway_context"
    details, fingerprint = _approval_details(
        _TOOL_NAME,
        normalized_arguments,
        session_key=normalized_session,
        binding=binding,
        operation_type="messaging.send_file",
        risk_level=HIGH,
        reason=reason,
        decision_source=decision_source,
        allowed_scopes=("once",),
    )
    return ApprovalAssessment(
        tool_name=_TOOL_NAME,
        decision=decision,
        risk_level=HIGH,
        fingerprint=fingerprint,
        reason=reason,
        normalized_arguments=normalized_arguments,
        details=details,
        normalized_path=binding["file_snapshot"]["abs_path"],
        session_key=normalized_session,
        error_type=error_type,
        error=error,
        fatal=fatal,
    )
