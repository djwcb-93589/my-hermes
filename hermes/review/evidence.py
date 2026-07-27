"""Memory Review 与 Skill Review 共用的无状态证据安全工具。"""

from __future__ import annotations

import json
from collections.abc import Mapping

from hermes.redaction import redact_explicit_secrets


TRUNCATED_MARKER = "[truncated]"

_SENSITIVE_ARGUMENT_KEYS = frozenset({
    "api_key",
    "apikey",
    "authorization",
    "captcha",
    "credential",
    "credentials",
    "otp",
    "one_time_code",
    "onetimecode",
    "passcode",
    "passwd",
    "password",
    "secret",
    "sms_code",
    "smscode",
    "token",
    "verificationcode",
    "verification_code",
    "verify_code",
    "验证码",
    "口令",
    "密码",
})
_TOOL_OMITTED_ARGUMENT_PATHS = {
    "browser_type": {
        ("text",): "input",
    },
    "browser_console": {
        ("expression",): "code",
    },
    "memory": {
        ("content",): "content",
        ("old_text",): "patch text",
    },
    "skill_manage": {
        ("body",): "content",
        ("content",): "content",
        ("old_text",): "patch text",
        ("new_text",): "patch text",
    },
}
_FILE_CONTENT_ARGUMENTS = frozenset({"content", "find", "replace"})
_FILE_CONTENT_ACTIONS = frozenset({"write", "append", "replace"})
_GENERIC_CONTENT_ARGUMENTS = frozenset({"body", "content", "text"})
_KNOWN_INTERNAL_USER_CONTENTS = frozenset({
    "please continue from where you left off.",
    "[continue]",
    "<continue>",
    "[approval_resume]",
    "[approval-resume]",
})
_KNOWN_INTERNAL_USER_PREFIXES = (
    "[CONTEXT COMPACTION]",
    "[APPROVAL RESUME]",
    "[APPROVAL_RESUME]",
    "[BACKGROUND REVIEW]",
    "[REVIEW INSTRUCTION]",
)
_INTERNAL_METADATA_VALUES = frozenset({
    "approval-resume",
    "approval_resume",
    "background-review",
    "background_review",
    "context-compaction",
    "context_compaction",
    "continuation",
    "framework",
    "internal",
    "review",
    "system",
})
_INTERNAL_METADATA_KEYS = (
    "gateway_internal_task",
    "message_source",
    "message_type",
    "origin",
    "source",
    "task_kind",
    "type",
)


def truncate_evidence_text(text: str, *, limit: int) -> str:
    """按字符上限裁剪文本，并显式标记证据不完整。"""
    if len(text) <= limit:
        return text
    marker = f"\n{TRUNCATED_MARKER}"
    if limit <= len(marker):
        return marker[:limit]
    return f"{text[:limit - len(marker)].rstrip()}{marker}"


def normalize_evidence_text(value: object, *, limit: int) -> str:
    """将任意证据转成脱敏、限长且不含二进制正文的文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            text = str(value)
    if "\x00" in text:
        return "[binary content omitted]"
    return truncate_evidence_text(
        redact_explicit_secrets(text).strip(),
        limit=limit,
    )


def _metadata_indicates_internal(message: Mapping) -> bool:
    """优先使用已有消息元数据识别框架生成内容。"""
    candidates: list[Mapping] = [message]
    for container_key in ("metadata", "_meta"):
        nested = message.get(container_key)
        if isinstance(nested, Mapping):
            candidates.append(nested)

    for candidate in candidates:
        for flag_key in (
            "framework_generated",
            "internal",
            "is_internal",
            "synthetic",
        ):
            if candidate.get(flag_key) is True:
                return True
        for metadata_key in _INTERNAL_METADATA_KEYS:
            value = candidate.get(metadata_key)
            if not isinstance(value, str):
                continue
            normalized = value.strip().lower().replace(" ", "_")
            if normalized in _INTERNAL_METADATA_VALUES:
                return True
    return False


def is_internal_user_message(message: Mapping, content: str) -> bool:
    """集中识别 continuation、审批恢复和其他框架 user 协议消息。"""
    if _metadata_indicates_internal(message):
        return True
    stripped = content.strip()
    if not stripped:
        return True
    if stripped.lower() in _KNOWN_INTERNAL_USER_CONTENTS:
        return True
    upper_content = stripped.upper()
    if any(
        upper_content.startswith(prefix)
        for prefix in _KNOWN_INTERNAL_USER_PREFIXES
    ):
        return True
    try:
        payload = json.loads(stripped)
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, Mapping):
        return False
    if _metadata_indicates_internal(payload):
        return True
    return (
        payload.get("approval_required") is True
        and isinstance(payload.get("approval_request"), Mapping)
    )


def is_explicit_tool_error(content: str) -> bool:
    """只把工具明确返回的错误状态归为工具错误。"""
    if content.lstrip().lower().startswith("(error:"):
        return True
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and (
        payload.get("ok") is False
        or "error" in payload
        or bool(payload.get("error_type"))
    )


def _normalized_argument_key(key: object) -> str:
    """统一参数键格式，仅用于选择省略规则。"""
    return str(key).strip().lower().replace("-", "_")


def _is_sensitive_argument_key(key: object) -> bool:
    """按明确的凭据字段名无条件隐藏参数值。"""
    normalized = _normalized_argument_key(key)
    if normalized in _SENSITIVE_ARGUMENT_KEYS:
        return True
    return (
        normalized.endswith("_password")
        or normalized.endswith("_secret")
        or normalized.endswith("_token")
        or normalized.endswith("_verification_code")
    )


def _omitted_value_summary(value: object, *, label: str) -> str:
    """仅说明敏感或大段参数存在，并保留可核对的字符数量。"""
    if isinstance(value, str):
        return f"[{label} omitted; chars={len(value)}]"
    return f"[{label} omitted]"


def _tool_argument_override(
    tool_name: str,
    path: tuple[str, ...],
    value: object,
    root_arguments: Mapping,
) -> str | None:
    """集中应用按工具名和参数路径定义的省略、摘要规则。"""
    normalized_tool = tool_name.strip().lower()
    if path and _is_sensitive_argument_key(path[-1]):
        return "[secret omitted]"

    configured_paths = _TOOL_OMITTED_ARGUMENT_PATHS.get(normalized_tool, {})
    label = configured_paths.get(path)
    if label == "input":
        return "[input omitted]"
    if label is not None:
        return _omitted_value_summary(value, label=label)

    if normalized_tool == "file" and len(path) == 1:
        action = str(root_arguments.get("action", "")).strip().lower()
        if action in _FILE_CONTENT_ACTIONS and path[0] in _FILE_CONTENT_ARGUMENTS:
            return _omitted_value_summary(value, label="file content")

    if normalized_tool == "terminal" and path == ("command",):
        return truncate_evidence_text(
            redact_explicit_secrets(str(value)),
            limit=360,
        )

    tools_with_explicit_rules = (
        set(_TOOL_OMITTED_ARGUMENT_PATHS)
        | {"file", "terminal"}
    )
    if (
        normalized_tool not in tools_with_explicit_rules
        and path
        and path[-1] in _GENERIC_CONTENT_ARGUMENTS
    ):
        return _omitted_value_summary(value, label="content")
    return None


def _compact_value(
    value: object,
    *,
    tool_name: str,
    path: tuple[str, ...] = (),
    root_arguments: Mapping,
    depth: int = 0,
) -> object:
    """按工具规则压缩参数，并对未知工具使用保守限制。"""
    override = _tool_argument_override(
        tool_name,
        path,
        value,
        root_arguments,
    )
    if override is not None:
        return override
    if depth >= 2:
        return "[nested value omitted]"
    if isinstance(value, Mapping):
        compact: dict[str, object] = {}
        for index, (child_key, child_value) in enumerate(value.items()):
            if index >= 12:
                compact[TRUNCATED_MARKER] = True
                break
            compact[str(child_key)] = _compact_value(
                child_value,
                tool_name=tool_name,
                path=path + (_normalized_argument_key(child_key),),
                root_arguments=root_arguments,
                depth=depth + 1,
            )
        return compact
    if isinstance(value, (list, tuple)):
        compact_items = [
            _compact_value(
                item,
                tool_name=tool_name,
                path=path + (str(index),),
                root_arguments=root_arguments,
                depth=depth + 1,
            )
            for index, item in enumerate(value[:6])
        ]
        if len(value) > 6:
            compact_items.append(TRUNCATED_MARKER)
        return compact_items
    if isinstance(value, str):
        return truncate_evidence_text(
            redact_explicit_secrets(value),
            limit=240,
        )
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return truncate_evidence_text(str(value), limit=240)


def compact_tool_argument_value(
    tool_name: str,
    arguments: object,
) -> object:
    """将工具参数解析并压缩为可继续选择字段的安全结构。"""
    if isinstance(arguments, str):
        try:
            payload = json.loads(arguments)
        except (TypeError, ValueError):
            return redact_explicit_secrets(arguments)
    else:
        payload = arguments
    if not isinstance(payload, Mapping):
        return normalize_evidence_text(payload, limit=240)

    return _compact_value(
        payload,
        tool_name=tool_name,
        root_arguments=payload,
    )


def compact_tool_arguments(
    tool_name: str,
    arguments: object,
    *,
    limit: int,
) -> str:
    """保留工具目标和动作，按共享安全规则省略大段或敏感参数。"""
    compact_value = compact_tool_argument_value(tool_name, arguments)
    return normalize_evidence_text(compact_value, limit=limit)


__all__ = [
    "TRUNCATED_MARKER",
    "compact_tool_argument_value",
    "compact_tool_arguments",
    "is_explicit_tool_error",
    "is_internal_user_message",
    "normalize_evidence_text",
    "truncate_evidence_text",
]
