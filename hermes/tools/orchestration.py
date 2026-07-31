"""把 orchestration_run 工具参数适配为独立编排应用调用。"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping

from hermes.orchestration.application import (
    OrchestrationApplication,
    OrchestrationRunReport,
    OrchestrationRunRequest,
)
from hermes.orchestration.composition import (
    OrchestrationRuntimeSettings,
    build_orchestration_application,
)
from hermes.orchestration.errors import (
    OrchestrationError,
    OrchestrationPersistenceError,
    OrchestrationValidationError,
    WorkflowRunnerError,
)
from hermes.orchestration.models import (
    TaskCreateSpec,
    WorkflowCreateSpec,
    plain_json_object,
)
from hermes.tool_declarations.orchestration import TOOL_DECLARATIONS
from hermes.tools import ToolRegistry, register_declared_handlers


logger = logging.getLogger(__name__)

_MAX_TITLE_LENGTH = 500
_MAX_TEXT_LENGTH = 100_000
_MAX_KEY_LENGTH = 128
_MAX_ROLE_LENGTH = 128
_MAX_SESSION_KEY_LENGTH = 512
_MAX_ATTEMPTS = 100
_MAX_PRIORITY_ABS = 1_000_000
_MAX_METADATA_PROPERTIES = 256
_MAX_OUTPUT_CHARS = 8_000_000
_ALLOWED_ROLES = frozenset({
    "researcher",
    "engineer",
    "reviewer",
    "synthesizer",
})
_TOP_LEVEL_KEYS = frozenset({
    "title",
    "goal",
    "tasks",
    "result_task_key",
    "max_concurrency",
})
_TASK_KEYS = frozenset({
    "key",
    "title",
    "prompt",
    "role",
    "depends_on",
    "priority",
    "max_attempts",
    "input_metadata",
})
_RUNTIME_OVERRIDE_KEYS = frozenset({
    "application",
    "client",
    "database_path",
    "db_path",
    "execution_registry",
    "executor",
    "isolated_agent_executor",
    "model_client",
    "process_manager",
    "registry",
    "runner_factory",
    "service",
    "settings",
    "task_executor",
})


def _required_text(
    value: object,
    field_name: str,
    *,
    maximum: int,
) -> str:
    if type(value) is not str or not value.strip():
        raise OrchestrationValidationError(
            f"{field_name} must be a non-empty string"
        )
    if len(value) > maximum:
        raise OrchestrationValidationError(
            f"{field_name} exceeds its length limit"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise OrchestrationValidationError(
            f"{field_name} must contain valid Unicode"
        ) from exc
    return value


def _parse_task(
    value: object,
    *,
    maximum_dependencies: int,
) -> TaskCreateSpec:
    if not isinstance(value, Mapping):
        raise OrchestrationValidationError("tasks must contain JSON objects")
    unknown_keys = set(value) - _TASK_KEYS
    if unknown_keys:
        raise OrchestrationValidationError(
            "task contains unsupported fields"
        )
    role = _required_text(
        value.get("role"),
        "task role",
        maximum=_MAX_ROLE_LENGTH,
    )
    if role not in _ALLOWED_ROLES:
        raise OrchestrationValidationError("task role is not supported")

    depends_on = value.get("depends_on", ())
    if not isinstance(depends_on, (list, tuple)):
        raise OrchestrationValidationError(
            "task depends_on must be an array"
        )
    if len(depends_on) > maximum_dependencies:
        raise OrchestrationValidationError(
            "task dependency count exceeds its limit"
        )
    normalized_dependencies = tuple(
        _required_text(
            dependency,
            "task dependency key",
            maximum=_MAX_KEY_LENGTH,
        )
        for dependency in depends_on
    )
    if len(normalized_dependencies) != len(set(normalized_dependencies)):
        raise OrchestrationValidationError(
            "task dependencies must not contain duplicates"
        )

    priority = value.get("priority", 0)
    if type(priority) is not int or not (
        -_MAX_PRIORITY_ABS <= priority <= _MAX_PRIORITY_ABS
    ):
        raise OrchestrationValidationError(
            "task priority is outside the supported range"
        )
    max_attempts = value.get("max_attempts", 1)
    if (
        type(max_attempts) is not int
        or not 1 <= max_attempts <= _MAX_ATTEMPTS
    ):
        raise OrchestrationValidationError(
            "task max_attempts must be within its limit"
        )
    input_metadata = value.get("input_metadata", {})
    if not isinstance(input_metadata, Mapping):
        raise OrchestrationValidationError(
            "task input_metadata must be a JSON object"
        )
    if len(input_metadata) > _MAX_METADATA_PROPERTIES:
        raise OrchestrationValidationError(
            "task input_metadata contains too many properties"
        )
    return TaskCreateSpec(
        key=_required_text(
            value.get("key"),
            "task key",
            maximum=_MAX_KEY_LENGTH,
        ),
        title=_required_text(
            value.get("title"),
            "task title",
            maximum=_MAX_TITLE_LENGTH,
        ),
        prompt=_required_text(
            value.get("prompt"),
            "task prompt",
            maximum=_MAX_TEXT_LENGTH,
        ),
        role=role,
        depends_on=normalized_dependencies,
        priority=priority,
        max_attempts=max_attempts,
        workdir=None,
        input_metadata=plain_json_object(
            input_metadata,
            field_name="input_metadata",
        ),
    )


def _parse_request(
    args: object,
    *,
    settings: OrchestrationRuntimeSettings,
    created_by_session: str | None,
) -> OrchestrationRunRequest:
    if not isinstance(args, Mapping):
        raise OrchestrationValidationError("arguments must be a JSON object")
    if set(args) - _TOP_LEVEL_KEYS:
        raise OrchestrationValidationError(
            "arguments contain unsupported fields"
        )
    tasks_value = args.get("tasks")
    if not isinstance(tasks_value, (list, tuple)):
        raise OrchestrationValidationError("tasks must be an array")
    if not 1 <= len(tasks_value) <= settings.max_tasks_per_workflow:
        raise OrchestrationValidationError(
            "workflow task count exceeds the configured limit"
        )
    tasks = tuple(
        _parse_task(
            task,
            maximum_dependencies=settings.max_tasks_per_workflow,
        )
        for task in tasks_value
    )
    result_task_key_value = args.get("result_task_key")
    result_task_key = (
        None
        if result_task_key_value is None
        else _required_text(
            result_task_key_value,
            "result_task_key",
            maximum=_MAX_KEY_LENGTH,
        )
    )
    max_concurrency = args.get(
        "max_concurrency",
        settings.default_max_concurrency,
    )
    if (
        type(max_concurrency) is not int
        or not 1 <= max_concurrency <= settings.maximum_max_concurrency
    ):
        raise OrchestrationValidationError(
            "max_concurrency exceeds the configured limit"
        )
    workflow = WorkflowCreateSpec(
        title=_required_text(
            args.get("title"),
            "title",
            maximum=_MAX_TITLE_LENGTH,
        ),
        goal=_required_text(
            args.get("goal"),
            "goal",
            maximum=_MAX_TEXT_LENGTH,
        ),
        created_by_session=created_by_session,
        tasks=tasks,
    )
    return OrchestrationRunRequest(
        workflow=workflow,
        result_task_key=result_task_key,
        max_concurrency=max_concurrency,
    )


def _created_by_session(runtime_kwargs: Mapping[str, object]) -> str | None:
    value = runtime_kwargs.get("session_key")
    if (
        type(value) is str
        and value.strip()
        and len(value) <= _MAX_SESSION_KEY_LENGTH
    ):
        return value
    return None


def _safe_tool_context(
    runtime_kwargs: Mapping[str, object],
) -> dict[str, object]:
    """只转交无身份凭证、数据库路径或 durable fencing 的来源标签。"""

    context: dict[str, object] = {}
    source = runtime_kwargs.get("source")
    if type(source) is str and 0 < len(source) <= 128:
        context["source"] = source
    gateway_platform = runtime_kwargs.get("gateway_platform")
    if type(gateway_platform) is str and 0 < len(gateway_platform) <= 128:
        context["gateway_platform"] = gateway_platform
    gateway_context = runtime_kwargs.get("gateway_context")
    if type(gateway_context) is bool:
        context["gateway_context"] = gateway_context
    return context


def _plain_metadata(
    value: Mapping[str, object] | None,
) -> dict[str, object] | None:
    return (
        None
        if value is None
        else plain_json_object(value, field_name="result_metadata")
    )


def _report_payload(report: OrchestrationRunReport) -> dict[str, object]:
    workflow_status = report.workflow_status
    ok = (
        report.execution_kind.value == "completed"
        and workflow_status is not None
        and workflow_status.value == "completed"
    )
    return {
        "ok": ok,
        "status": report.execution_kind.value,
        "workflow_id": report.workflow_id,
        "workflow_status": (
            None if workflow_status is None else workflow_status.value
        ),
        "snapshot_fresh": report.snapshot_fresh,
        "result_task_key": report.result_task_key,
        "result_summary": report.result_summary,
        "result_metadata": _plain_metadata(report.result_metadata),
        "scheduled_task_ids": list(report.scheduled_task_ids),
        "tasks": [
            {
                "task_id": task.task_id,
                "key": task.task_key,
                "title": task.title,
                "role": task.role,
                "status": task.status.value,
                "result_summary": task.result_summary,
                "result_metadata": _plain_metadata(task.result_metadata),
                "error_type": task.error_type,
                "error_message": task.error_message,
                "blocked_reason": task.blocked_reason,
            }
            for task in report.tasks
        ],
        "error_type": report.error_type,
        "error": report.error_message,
    }


def _error_payload(
    *,
    status: str,
    error_type: str,
    error: str,
) -> dict[str, object]:
    return {
        "ok": False,
        "status": status,
        "workflow_id": None,
        "workflow_status": None,
        "snapshot_fresh": False,
        "result_task_key": None,
        "result_summary": None,
        "result_metadata": None,
        "scheduled_task_ids": [],
        "tasks": [],
        "error_type": error_type,
        "error": error,
    }


def _json_dumps(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    if len(encoded) <= _MAX_OUTPUT_CHARS:
        return encoded
    bounded = _error_payload(
        status="unavailable",
        error_type="orchestration_report_too_large",
        error="orchestration report exceeds the tool output limit",
    )
    bounded["workflow_id"] = payload.get("workflow_id")
    bounded["workflow_status"] = payload.get("workflow_status")
    bounded["snapshot_fresh"] = bool(payload.get("snapshot_fresh", False))
    bounded["result_task_key"] = payload.get("result_task_key")
    return json.dumps(
        bounded,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def handle_orchestration_run(
    args: object,
    *,
    application: OrchestrationApplication,
    settings: OrchestrationRuntimeSettings,
    **runtime_kwargs,
) -> str:
    """解析公开参数并同步调用应用，不直接接触 Store、Runner 或 Claim。"""

    try:
        request = _parse_request(
            args,
            settings=settings,
            created_by_session=_created_by_session(runtime_kwargs),
        )
        report = application.run(
            request,
            cancel_checker=runtime_kwargs.get("cancel_checker"),
            hook_registry=runtime_kwargs.get("hook_registry"),
            parent_run_id=runtime_kwargs.get("parent_run_id"),
            tool_context=_safe_tool_context(runtime_kwargs),
        )
        if not isinstance(report, OrchestrationRunReport):
            raise WorkflowRunnerError(
                "orchestration application returned an invalid report"
            )
        return _json_dumps(_report_payload(report))
    except OrchestrationValidationError as exc:
        return _json_dumps(_error_payload(
            status="invalid_args",
            error_type="invalid_args",
            error=str(exc),
        ))
    except OrchestrationPersistenceError:
        return _json_dumps(_error_payload(
            status="unavailable",
            error_type="orchestration_persistence_unavailable",
            error="orchestration persistence is unavailable",
        ))
    except WorkflowRunnerError:
        return _json_dumps(_error_payload(
            status="internal_error",
            error_type="workflow_runner_error",
            error="workflow execution could not produce a stable report",
        ))
    except OrchestrationError:
        return _json_dumps(_error_payload(
            status="internal_error",
            error_type="orchestration_error",
            error="orchestration execution failed",
        ))
    except Exception as exc:
        logger.error(
            "orchestration_run failed: exception_type=%s",
            type(exc).__name__,
        )
        return _json_dumps(_error_payload(
            status="internal_error",
            error_type="internal_error",
            error="orchestration execution failed",
        ))


def register(
    registry,
    *,
    execution_registry=None,
    process_manager=None,
    application=None,
    settings=None,
):
    """注册 Handler，并把 Worker Runtime 固定到正式应用依赖。"""

    registration_registry = registry
    active_execution_registry = (
        registration_registry
        if execution_registry is None
        else execution_registry
    )
    active_settings = (
        OrchestrationRuntimeSettings() if settings is None else settings
    )
    if not isinstance(registration_registry, ToolRegistry):
        raise TypeError("registry must be a ToolRegistry")
    if not isinstance(active_execution_registry, ToolRegistry):
        raise TypeError("execution_registry must be a ToolRegistry")
    if not isinstance(active_settings, OrchestrationRuntimeSettings):
        raise TypeError("settings must be OrchestrationRuntimeSettings")

    if application is None:
        active_process_manager = process_manager
        if active_process_manager is None:
            from hermes.processes import (
                process_manager as default_process_manager,
            )

            active_process_manager = default_process_manager
        from hermes.config import (
            DB_PATH,
            MAX_CHILD_ITERATIONS,
            MODEL,
            MODEL_MAX_OUTPUT_TOKENS,
            client,
        )

        active_application = build_orchestration_application(
            db_path=DB_PATH,
            execution_registry=active_execution_registry,
            process_manager=active_process_manager,
            model_client=client,
            model=MODEL,
            max_output_tokens=MODEL_MAX_OUTPUT_TOKENS,
            max_child_iterations=MAX_CHILD_ITERATIONS,
            settings=active_settings,
            capability_validation_registry=registration_registry,
        )
    else:
        if not callable(getattr(application, "run", None)):
            raise TypeError("application must provide run()")
        active_application = application

    def orchestration_run_handler(args, **kwargs):
        """移除可覆盖注册期所有权的运行时参数。"""

        trusted_runtime_kwargs = dict(kwargs)
        for key in _RUNTIME_OVERRIDE_KEYS:
            trusted_runtime_kwargs.pop(key, None)
        return handle_orchestration_run(
            args,
            application=active_application,
            settings=active_settings,
            **trusted_runtime_kwargs,
        )

    register_declared_handlers(
        registration_registry,
        TOOL_DECLARATIONS,
        {"orchestration_run": orchestration_run_handler},
    )


__all__ = ["handle_orchestration_run", "register"]
