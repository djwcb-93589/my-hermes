"""统一处理 Gateway 与 Cron 的中断工具执行恢复策略。"""

from __future__ import annotations

import json
from typing import Any

from hermes.durable_tool_dispatcher import DurableToolDispatcher, tool_output_failed
from hermes.persistence.tool_execution import (
    mark_tool_execution_unknown,
    save_recovered_tool_execution_result,
)


def _safe_unknown_output(record: dict, reason: str) -> str:
    return json.dumps(
        {
            "ok": False,
            "error_type": "tool_execution_unknown",
            "fatal": True,
            "error": reason,
            "tool_name": record["tool_name"],
            "execution_id": record["execution_id"],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _status_check_result(value: Any) -> tuple[str, str]:
    """把工具提供的受信任状态检查结果规范为 Journal 终态和 Tool Result。"""
    if isinstance(value, str):
        return ("failed" if tool_output_failed(value) else "succeeded", value)
    if not isinstance(value, dict):
        raise ValueError("status_check must return a string or mapping")
    status = str(value.get("status", "")).strip().lower()
    output = value.get("output")
    if status not in {"succeeded", "failed", "unknown"}:
        raise ValueError("status_check returned an invalid status")
    if not isinstance(output, str):
        output = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return status, output


class ToolExecutionRecoveryService:
    """根据注册元数据恢复一组已按入口范围筛选的 Journal 记录。"""

    def __init__(
        self,
        registry,
        dispatcher: DurableToolDispatcher,
        before_recover=None,
        **dispatch_kwargs,
    ):
        self.registry = registry
        self.dispatcher = dispatcher
        self.before_recover = before_recover
        self.dispatch_kwargs = dispatch_kwargs

    def recover(self, records: list[dict]) -> list[dict]:
        """逐条处理记录；一条失败不会阻塞同范围的其他恢复判断。"""
        recovered: list[dict] = []
        for record in records:
            if self.before_recover is not None:
                self.before_recover()
            recovered.append(self.recover_one(record))
        return recovered

    def recover_one(self, record: dict) -> dict:
        """执行 retry_safe、unknown_on_crash 或 status_check 的统一规则。"""
        entry = self.registry.get_entry(str(record["tool_name"]))
        if entry is None:
            return self._save_unknown(
                record,
                "The original tool is no longer registered, so its interrupted operation was not retried.",
            )

        policy = str(record.get("recovery_policy", ""))
        if policy == "status_check" and entry.status_check is not None:
            return self._recover_by_status_check(record, entry.status_check)
        if policy == "retry_safe" and entry.retry_safe:
            return self._recover_by_retry(record)
        return self._save_unknown(
            record,
            "The process stopped before this tool call produced a confirmed result. "
            "This operation is not marked safe to retry automatically.",
        )

    def _recover_by_retry(self, record: dict) -> dict:
        try:
            output = self.dispatcher.retry_record(record, **self.dispatch_kwargs)
        except Exception as exc:
            return self._save_unknown(
                record,
                f"Safe retry could not complete: {type(exc).__name__}.",
            )
        status = "failed" if tool_output_failed(output) else "succeeded"
        return self.dispatcher._call(
            save_recovered_tool_execution_result,
            record["execution_id"],
            status=status,
            output=output,
        )

    def _recover_by_status_check(self, record: dict, status_check) -> dict:
        try:
            checked = status_check(record, record.get("external_operation_id"))
            status, output = _status_check_result(checked)
        except Exception as exc:
            return self._save_unknown(
                record,
                f"External operation status could not be confirmed: {type(exc).__name__}.",
            )
        if status == "unknown":
            return self._save_unknown(record, output)
        return self.dispatcher._call(
            save_recovered_tool_execution_result,
            record["execution_id"],
            status=status,
            output=output,
        )

    def _save_unknown(self, record: dict, reason: str) -> dict:
        output = _safe_unknown_output(record, reason)
        try:
            self.dispatcher._call(
                mark_tool_execution_unknown,
                record["execution_id"],
                result={"output": output},
            )
        except Exception:
            pass
        return self.dispatcher._call(
            save_recovered_tool_execution_result,
            record["execution_id"],
            status="unknown",
            output=output,
        )
