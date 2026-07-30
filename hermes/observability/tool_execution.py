"""现有 Tool Execution Journal 的安全只读投影。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass


def _required_text(record: Mapping[str, object], field_name: str) -> str:
    value = record.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"tool execution {field_name} is invalid")
    return value.strip()


def _optional_text(record: Mapping[str, object], field_name: str) -> str | None:
    value = record.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"tool execution {field_name} is invalid")
    return value.strip()


def _timestamp(record: Mapping[str, object], field_name: str) -> float:
    value = record.get(field_name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"tool execution {field_name} is invalid")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"tool execution {field_name} is invalid")
    return normalized


def _attempt_count(record: Mapping[str, object]) -> int:
    value = record.get("attempt_count")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("tool execution attempt_count is invalid")
    return value


def _has_result(record: Mapping[str, object]) -> bool:
    """仅根据已读取记录中的结果字段判断存在性，不解析或保存结果正文。"""
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
    if "external_operation_id" not in record:
        raise ValueError("tool execution external_operation_id is missing")
    value = record["external_operation_id"]
    if value is not None and (
        type(value) is not str or not value.strip()
    ):
        raise ValueError("tool execution external_operation_id is invalid")
    return value is not None


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


def project_tool_execution(record: Mapping[str, object]) -> ToolExecutionSummary:
    """把一条已读取的 Journal 记录转换为不会泄漏执行内容的摘要。"""
    if not isinstance(record, Mapping):
        raise TypeError("tool execution record must be a mapping")
    return ToolExecutionSummary(
        execution_id=_required_text(record, "execution_id"),
        environment=_required_text(record, "environment"),
        session_id=_optional_text(record, "session_id"),
        source_message_id=_optional_text(record, "source_message_id"),
        cron_run_id=_optional_text(record, "cron_run_id"),
        tool_call_id=_required_text(record, "tool_call_id"),
        tool_name=_required_text(record, "tool_name"),
        recovery_policy=_required_text(record, "recovery_policy"),
        status=_required_text(record, "status"),
        attempt_count=_attempt_count(record),
        has_result=_has_result(record),
        has_external_operation=_has_external_operation(record),
        created_at=_timestamp(record, "created_at"),
        updated_at=_timestamp(record, "updated_at"),
    )
