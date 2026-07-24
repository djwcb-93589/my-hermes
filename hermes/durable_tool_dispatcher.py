"""为公共工具分发提供可选的执行 Journal 包装。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes.persistence.schema import init_db
from hermes.persistence.tool_execution import (
    complete_tool_execution,
    create_tool_execution,
    defer_tool_execution,
    fail_tool_execution,
    mark_tool_execution_unknown,
    retry_tool_execution,
    save_recovered_tool_execution_result,
    start_tool_execution,
)


def tool_output_failed(output: str) -> bool:
    """仅按公共 Tool Result 约定判断 handler 是否已明确返回失败。"""
    if output.lstrip().lower().startswith("(error:"):
        return True
    try:
        value = json.loads(output)
    except (TypeError, ValueError):
        return False
    return isinstance(value, dict) and (
        value.get("ok") is False
        or "error" in value
        or bool(value.get("error_type"))
        or value.get("fatal") is True
    )


def tool_output_awaits_approval(output: str) -> bool:
    """识别尚未执行、等待人工确认的公共 Tool Result。"""
    try:
        value = json.loads(output)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(value, dict)
        and value.get("status") == "awaiting_approval"
        and value.get("approval_required") is True
    )


@dataclass(frozen=True)
class DurableToolExecutionContext:
    """调用方提供的 Journal 范围，不会传递给具体工具 handler。"""

    environment: str
    session_id: str | None = None
    source_message_id: str | None = None
    cron_run_id: str | None = None
    gateway_lease_name: str | None = None
    gateway_instance_id: str | None = None
    gateway_lease_epoch: int | None = None
    connection: sqlite3.Connection | None = None
    database_path: str | None = None

    @classmethod
    def from_value(cls, value: object) -> "DurableToolExecutionContext | None":
        if value is None or value is False:
            return None
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict) or value.get("enabled", True) is not True:
            return None
        return cls(
            environment=str(value.get("environment", "")),
            session_id=value.get("session_id"),
            source_message_id=value.get("source_message_id"),
            cron_run_id=value.get("cron_run_id"),
            gateway_lease_name=value.get("gateway_lease_name"),
            gateway_instance_id=value.get("gateway_instance_id"),
            gateway_lease_epoch=value.get("gateway_lease_epoch"),
            connection=value.get("connection"),
            database_path=value.get("database_path"),
        )


class DurableToolDispatcher:
    """在不改变 handler 契约的前提下记录单次工具执行。"""

    def __init__(self, registry, context: DurableToolExecutionContext):
        self.registry = registry
        self.context = context
        if not self.context.environment.strip():
            raise ValueError("durable tool execution environment is required")
        if self.context.connection is None and not self.context.database_path:
            raise ValueError("durable tool execution persistence target is required")

    def dispatch(self, name: str, args: dict, *, tool_call_id: str, **kwargs) -> str:
        """记录调用开始、调用既有分发器，并保存确定或未知结果。"""
        entry = self.registry.get_entry(name)
        if entry is None:
            return self.registry.dispatch(name, args, **kwargs)
        if entry.status_check is not None:
            recovery_policy = "status_check"
        elif entry.retry_safe:
            recovery_policy = "retry_safe"
        elif entry.unknown_on_crash:
            recovery_policy = "unknown_on_crash"
        else:
            raise RuntimeError("tool durable recovery policy is unavailable")
        record = self._call(
            create_tool_execution,
            environment=self.context.environment,
            session_id=self.context.session_id,
            source_message_id=self.context.source_message_id,
            cron_run_id=self.context.cron_run_id,
            gateway_lease_name=self.context.gateway_lease_name,
            gateway_instance_id=self.context.gateway_instance_id,
            gateway_lease_epoch=self.context.gateway_lease_epoch,
            tool_call_id=tool_call_id,
            tool_name=name,
            arguments=args,
            recovery_policy=recovery_policy,
        )
        execution_id = record["execution_id"]
        self._call(start_tool_execution, execution_id)
        try:
            output = self.registry.dispatch(name, args, **kwargs)
        except Exception:
            self._mark_unknown_best_effort(execution_id)
            raise

        result = {"output": output}
        try:
            if tool_output_awaits_approval(output):
                self._call(defer_tool_execution, execution_id, result)
            elif tool_output_failed(output):
                self._call(fail_tool_execution, execution_id, result)
            else:
                self._call(complete_tool_execution, execution_id, result)
        except Exception:
            self._mark_unknown_best_effort(execution_id)
            raise
        return output

    def retry_record(self, record: dict, **kwargs) -> str:
        """用保存的参数和原 execution_id 执行已声明可安全重试的调用。"""
        self._call(retry_tool_execution, record["execution_id"])
        retry_kwargs = dict(kwargs)
        retry_kwargs.setdefault("session_key", record.get("session_id"))
        return self.dispatch(
            str(record["tool_name"]),
            record["arguments"],
            tool_call_id=str(record["tool_call_id"]),
            **retry_kwargs,
        )

    def _mark_unknown_best_effort(self, execution_id: str) -> None:
        try:
            self._call(mark_tool_execution_unknown, execution_id)
        except Exception:
            # Journal 已保持 running 时，未来恢复扫描仍可发现这次未确定执行。
            pass

    def _call(self, operation, *args, **kwargs):
        if self.context.connection is not None:
            return operation(self.context.connection, *args, **kwargs)
        path = Path(str(self.context.database_path))
        conn = init_db(str(path))
        try:
            return operation(conn, *args, **kwargs)
        finally:
            conn.close()
