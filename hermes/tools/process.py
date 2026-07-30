"""process 工具：按可信 session 管理 ProcessManager 中的后台进程。"""

from __future__ import annotations

import json
import math

from hermes.backends import INFRASTRUCTURE_CREDENTIAL_ENV_VARS
from hermes.processes import (
    ProcessError,
    ProcessNotFoundError,
    ProcessStatus,
    ProcessTerminationError,
    ProcessWaitCancelled,
)
from hermes.redaction import redact_terminal_output
from hermes.tool_declarations.process import TOOL_DECLARATIONS


_ACTIONS = frozenset({"list", "poll", "log", "wait", "kill"})
_ACTION_FIELDS = {
    "list": frozenset({"action", "include_finished"}),
    "poll": frozenset({"action", "process_id", "cursor", "limit"}),
    "log": frozenset({"action", "process_id", "cursor", "limit"}),
    "wait": frozenset({
        "action",
        "process_id",
        "timeout",
        "cursor",
        "limit",
    }),
    "kill": frozenset({
        "action",
        "process_id",
        "grace_seconds",
        "cursor",
        "limit",
    }),
}
_WAIT_ACTIVE_STATUSES = frozenset({
    ProcessStatus.STARTING,
    ProcessStatus.RUNNING,
})


class _InvalidProcessArguments(ValueError):
    """Process Tool 参数不满足当前 action 的严格契约。"""


def _json(payload: dict) -> str:
    """使用统一的非 ASCII 转义策略序列化工具结果。"""

    return json.dumps(payload, ensure_ascii=False)


def _error(error_type: str, error: str, **extra) -> str:
    """生成稳定且不携带底层异常文本的失败结果。"""

    return _json({
        "ok": False,
        "error_type": error_type,
        "error": error,
        **extra,
    })


def _validate_nonnegative_int(value, *, maximum: int | None = None) -> int:
    """校验非负整数；bool 不得作为整数使用。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _InvalidProcessArguments
    if maximum is not None and value > maximum:
        raise _InvalidProcessArguments
    return value


def _validate_bounded_number(value, *, maximum: float) -> float:
    """校验有限且位于允许范围内的 JSON number。"""

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value < 0
        or value > maximum
        or (isinstance(value, float) and not math.isfinite(value))
    ):
        raise _InvalidProcessArguments
    return float(value)


def _validate_process_args(args: object) -> dict:
    """按 action 校验允许字段、必需字段和数值边界。"""

    if not isinstance(args, dict):
        raise _InvalidProcessArguments
    action = args.get("action")
    if not isinstance(action, str) or action not in _ACTIONS:
        raise _InvalidProcessArguments
    if not set(args).issubset(_ACTION_FIELDS[action]):
        raise _InvalidProcessArguments

    validated: dict = {"action": action}
    if action == "list":
        include_finished = args.get("include_finished", True)
        if not isinstance(include_finished, bool):
            raise _InvalidProcessArguments
        validated["include_finished"] = include_finished
        return validated

    process_id = args.get("process_id")
    if not isinstance(process_id, str) or not process_id.strip():
        raise _InvalidProcessArguments
    validated["process_id"] = process_id
    validated["cursor"] = _validate_nonnegative_int(
        args.get("cursor", 0)
    )
    limit = _validate_nonnegative_int(
        args.get("limit", 20_000),
        maximum=20_000,
    )
    if limit < 1:
        raise _InvalidProcessArguments
    validated["limit"] = limit

    if action == "wait":
        validated["timeout"] = _validate_bounded_number(
            args.get("timeout", 30),
            maximum=300,
        )
    elif action == "kill":
        validated["grace_seconds"] = _validate_bounded_number(
            args.get("grace_seconds", 2),
            maximum=30,
        )
    return validated


def _snapshot_payload(snapshot) -> dict:
    """序列化 ProcessSnapshot 的安全字段，刻意排除 command。"""

    return {
        "process_id": snapshot.process_id,
        "pid": snapshot.pid,
        "backend_type": snapshot.backend_type,
        "cwd": snapshot.cwd,
        "status": snapshot.status.value,
        "exit_code": snapshot.exit_code,
        "started_at": snapshot.started_at,
        "finished_at": snapshot.finished_at,
        "completion_reason": snapshot.completion_reason,
        "termination_source": snapshot.termination_source,
        "output_base_cursor": snapshot.output_base_cursor,
        "output_end_cursor": snapshot.output_end_cursor,
    }


def _safe_log_output(snapshot, log_result) -> str:
    """只脱敏响应副本；cursor 继续使用 ProcessManager 的原始位置。"""

    return redact_terminal_output(
        log_result.output,
        snapshot.command,
        infrastructure_env_names=INFRASTRUCTURE_CREDENTIAL_ENV_VARS,
    )


def _log_page_payload(snapshot, log_result) -> dict:
    """构造不重新计算 cursor 的安全日志分页字段。"""

    return {
        "output": _safe_log_output(snapshot, log_result),
        "requested_cursor": log_result.requested_cursor,
        "available_from_cursor": log_result.available_from_cursor,
        "next_cursor": log_result.next_cursor,
        "output_truncated": log_result.output_truncated,
    }


def _kill_message(status: ProcessStatus) -> str:
    """根据 ProcessManager 最终状态描述 kill 的真实结果。"""

    if status is ProcessStatus.KILLED:
        return "Process termination completed"
    if status is ProcessStatus.EXITED:
        return "Process had already exited"
    if status is ProcessStatus.LOST:
        return "Lost process resources were cleaned up"
    if status is ProcessStatus.FAILED_START:
        return "Failed-start process resources were cleaned up"
    return "Process termination state is unchanged"


def _run_process_action(
    validated: dict,
    *,
    process_manager,
    session_key: str,
    cancel_checker,
) -> str:
    """把已校验 action 映射到 ProcessManager 的公开方法。"""

    action = validated["action"]
    if action == "list":
        snapshots = process_manager.list(
            session_key,
            include_finished=validated["include_finished"],
        )
        processes = [
            _snapshot_payload(snapshot)
            for snapshot in snapshots
        ]
        return _json({
            "ok": True,
            "action": "list",
            "processes": processes,
            "count": len(processes),
        })

    process_id = validated["process_id"]
    cursor = validated["cursor"]
    limit = validated["limit"]
    if action == "poll":
        snapshot = process_manager.poll(session_key, process_id)
        log_result = process_manager.log(
            session_key,
            process_id,
            cursor=cursor,
            limit=limit,
        )
        return _json({
            "ok": True,
            "action": "poll",
            "process": _snapshot_payload(snapshot),
            **_log_page_payload(snapshot, log_result),
        })

    if action == "log":
        snapshot = process_manager.poll(session_key, process_id)
        log_result = process_manager.log(
            session_key,
            process_id,
            cursor=cursor,
            limit=limit,
        )
        return _json({
            "ok": True,
            "action": "log",
            "process_id": log_result.process_id,
            "status": log_result.status.value,
            **_log_page_payload(snapshot, log_result),
            "exit_code": log_result.exit_code,
        })

    if action == "wait":
        snapshot = process_manager.wait(
            session_key,
            process_id,
            timeout=validated["timeout"],
            cancel_checker=(
                cancel_checker if callable(cancel_checker) else None
            ),
        )
        log_result = process_manager.log(
            session_key,
            process_id,
            cursor=cursor,
            limit=limit,
        )
        return _json({
            "ok": True,
            "action": "wait",
            "timed_out": snapshot.status in _WAIT_ACTIVE_STATUSES,
            "process": _snapshot_payload(snapshot),
            **_log_page_payload(snapshot, log_result),
        })

    snapshot = process_manager.kill(
        session_key,
        process_id,
        grace_seconds=validated["grace_seconds"],
    )
    log_result = process_manager.log(
        session_key,
        process_id,
        cursor=cursor,
        limit=limit,
    )
    return _json({
        "ok": True,
        "action": "kill",
        "status": snapshot.status.value,
        "process": _snapshot_payload(snapshot),
        **_log_page_payload(snapshot, log_result),
        "message": _kill_message(snapshot.status),
    })


def run_process(args, *, process_manager=None, **kwargs) -> str:
    """执行当前可信 session 内的一次后台进程管理操作。"""

    session_key = kwargs.get("session_key")
    if not isinstance(session_key, str) or not session_key.strip():
        return _error(
            "missing_session",
            "Process management requires an active session",
        )
    if process_manager is None or not callable(
        getattr(process_manager, "poll", None)
    ):
        return _error(
            "internal_error",
            "Process operation failed unexpectedly",
        )
    try:
        validated = _validate_process_args(args)
    except _InvalidProcessArguments:
        return _error(
            "invalid_args",
            "Invalid arguments for process action",
        )

    try:
        return _run_process_action(
            validated,
            process_manager=process_manager,
            session_key=session_key,
            cancel_checker=kwargs.get("cancel_checker"),
        )
    except ProcessNotFoundError:
        return _error(
            "process_not_found",
            "Process was not found in the current session",
        )
    except ProcessWaitCancelled:
        return _error(
            "cancelled",
            "Process wait was cancelled",
            process_terminated=False,
        )
    except ProcessTerminationError:
        return _error(
            "process_termination_failed",
            "Process termination could not be confirmed",
            retryable=True,
        )
    except ProcessError:
        return _error(
            "process_error",
            "Process operation failed",
        )
    except Exception:
        return _error(
            "internal_error",
            "Process operation failed unexpectedly",
        )


def _unknown_status_check() -> dict:
    """返回不会触发自动重放的保守恢复判断。"""

    return {
        "status": "unknown",
        "output": "Process operation result could not be confirmed",
    }


def _status_check(record: object, process_manager) -> dict:
    """仅确认中断的 kill 是否已达到可观察终态，不重新执行操作。"""

    if not isinstance(record, dict):
        return _unknown_status_check()
    arguments = record.get("arguments")
    session_key = record.get("session_id")
    if (
        not isinstance(arguments, dict)
        or arguments.get("action") != "kill"
        or not isinstance(session_key, str)
        or not session_key.strip()
    ):
        return _unknown_status_check()
    process_id = arguments.get("process_id")
    if not isinstance(process_id, str) or not process_id.strip():
        return _unknown_status_check()
    try:
        snapshot = process_manager.poll(session_key, process_id)
    except Exception:
        return _unknown_status_check()
    if snapshot.status not in {
        ProcessStatus.KILLED,
        ProcessStatus.EXITED,
        ProcessStatus.FAILED_START,
    }:
        return _unknown_status_check()
    return {
        "status": "succeeded",
        "output": _json({
            "ok": True,
            "action": "kill",
            "status": snapshot.status.value,
            "process": _snapshot_payload(snapshot),
            "message": _kill_message(snapshot.status),
        }),
    }


def register(registry, *, process_manager) -> None:
    """绑定共享 ProcessManager 并注册 Process Tool。"""

    if not callable(getattr(process_manager, "poll", None)):
        raise TypeError("process_manager must provide process operations")

    def handler(args, **kwargs):
        """运行时上下文不得覆盖注册阶段绑定的 Manager。"""

        kwargs.pop("process_manager", None)
        return run_process(
            args,
            process_manager=process_manager,
            **kwargs,
        )

    def status_check(record, _external_operation_id):
        """崩溃恢复只读检查状态，绝不重放 kill。"""

        return _status_check(record, process_manager)

    registry.register_declaration(
        TOOL_DECLARATIONS[0],
        handler,
        status_check=status_check,
    )


__all__ = ["register", "run_process"]
