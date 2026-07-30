"""现有 Tool Execution Journal 的安全只读投影。"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeAlias

from hermes.redaction import redact_explicit_secrets
from hermes.tool_policy import normalize_execution_environment


_RECOVERY_POLICIES = frozenset({
    "retry_safe",
    "unknown_on_crash",
    "status_check",
})
_TOOL_EXECUTION_STATUSES = frozenset({
    "prepared",
    "awaiting_approval",
    "running",
    "succeeded",
    "failed",
    "unknown",
})
_CONTROL_TEXT_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_MAX_SAFE_TEXT_LENGTH = 512


def _required_text(record: Mapping[str, object], field_name: str) -> str:
    value = record.get(field_name)
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > _MAX_SAFE_TEXT_LENGTH
        or _CONTROL_TEXT_RE.search(value)
        or any(character.isspace() for character in value)
        or value.startswith(("/", "\\"))
        or _WINDOWS_ABSOLUTE_PATH_RE.match(value)
        or "://" in value
        or redact_explicit_secrets(value) != value
    ):
        raise ValueError(f"tool execution {field_name} is invalid")
    return value


def _optional_text(record: Mapping[str, object], field_name: str) -> str | None:
    value = record.get(field_name)
    if value is None:
        return None
    return _required_text(record, field_name)


def _timestamp(record: Mapping[str, object], field_name: str) -> float:
    value = record.get(field_name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"tool execution {field_name} is invalid")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"tool execution {field_name} is invalid")
    return normalized


def _attempt_count(record: Mapping[str, object]) -> int:
    value = record.get("attempt_count")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("tool execution attempt_count is invalid")
    return value


def _environment(record: Mapping[str, object]) -> str:
    value = _required_text(record, "environment")
    try:
        return normalize_execution_environment(value).value
    except (TypeError, ValueError):
        raise ValueError("tool execution environment is invalid") from None


def _choice(
    record: Mapping[str, object],
    field_name: str,
    allowed: frozenset[str],
) -> str:
    value = _required_text(record, field_name)
    if value not in allowed:
        raise ValueError(f"tool execution {field_name} is invalid")
    return value


def _has_result(record: Mapping[str, object]) -> bool:
    """仅根据已读取记录中的结果字段判断存在性，不解析或保存结果正文。"""
    if "has_result" in record:
        return _stored_boolean(record["has_result"], "has_result")
    if "result" in record:
        return record["result"] is not None
    if "result_json" in record:
        value = record["result_json"]
        if value is not None and type(value) is not str:
            raise ValueError("tool execution result_json is invalid")
        return value is not None
    raise ValueError("tool execution result is missing")


def _has_external_operation(record: Mapping[str, object]) -> bool:
    """只保留外部操作是否已建立，绝不投影其内部标识。"""
    if "has_external_operation" in record:
        return _stored_boolean(
            record["has_external_operation"],
            "has_external_operation",
        )
    if "external_operation_id" not in record:
        raise ValueError("tool execution external_operation_id is missing")
    value = record["external_operation_id"]
    if value is not None and (
        type(value) is not str or not value.strip()
    ):
        raise ValueError("tool execution external_operation_id is invalid")
    return value is not None


def _stored_boolean(value: object, field_name: str) -> bool:
    """接收 Python bool 或 SQLite CASE 返回的精确 0/1。"""
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    raise ValueError(f"tool execution {field_name} is invalid")


@dataclass(frozen=True, slots=True)
class ToolExecutionSummary:
    """不含参数、结果、指纹和 fencing 身份的 Journal 摘要。"""

    execution_id: str
    environment: str
    session_id: str | None
    source_message_id: str | None
    cron_run_id: str | None
    tool_call_id: str
    tool_name: str
    recovery_policy: str
    status: str
    attempt_count: int
    has_result: bool
    has_external_operation: bool
    created_at: float
    updated_at: float


# 详情接口没有额外安全字段可公开，复用同一不可变投影避免复制模型。
ToolExecutionDetail: TypeAlias = ToolExecutionSummary


def project_tool_execution(record: Mapping[str, object]) -> ToolExecutionSummary:
    """把一条已读取的 Journal 记录转换为不会泄漏执行内容的摘要。"""
    if not isinstance(record, Mapping):
        raise TypeError("tool execution record must be a mapping")
    return ToolExecutionSummary(
        execution_id=_required_text(record, "execution_id"),
        environment=_environment(record),
        session_id=_optional_text(record, "session_id"),
        source_message_id=_optional_text(record, "source_message_id"),
        cron_run_id=_optional_text(record, "cron_run_id"),
        tool_call_id=_required_text(record, "tool_call_id"),
        tool_name=_required_text(record, "tool_name"),
        recovery_policy=_choice(
            record,
            "recovery_policy",
            _RECOVERY_POLICIES,
        ),
        status=_choice(
            record,
            "status",
            _TOOL_EXECUTION_STATUSES,
        ),
        attempt_count=_attempt_count(record),
        has_result=_has_result(record),
        has_external_operation=_has_external_operation(record),
        created_at=_timestamp(record, "created_at"),
        updated_at=_timestamp(record, "updated_at"),
    )
