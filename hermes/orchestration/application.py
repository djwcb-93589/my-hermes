"""创建并同步运行一个固定 Workflow 的独立应用用例。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from hermes.orchestration.errors import (
    OrchestrationPersistenceError,
    OrchestrationRunCreatedError,
    OrchestrationValidationError,
    WorkflowRunnerError,
)
from hermes.orchestration.models import (
    TaskCreateSpec,
    TaskRecord,
    TaskStatus,
    WorkflowCreateSpec,
    WorkflowStatus,
    freeze_json_object,
    plain_json_object,
)
from hermes.orchestration.roles import (
    AgentRoleDefinition,
    AgentRoleSpec,
    RoleResolver,
    RoleResolverFactory,
)
from hermes.orchestration.service import OrchestrationService
from hermes.orchestration.workflow_execution import (
    WorkflowExecutionKind,
    WorkflowExecutionResult,
    WorkflowRunner,
)


_MAX_APPLICATION_CONCURRENCY = 16
_MAX_AGENT_DEFINITIONS = 32
_MAX_TOTAL_AGENT_INSTRUCTIONS_CHARS = 1_000_000


def _copy_workflow_spec(spec: WorkflowCreateSpec) -> WorkflowCreateSpec:
    """重建完整创建计划，避免 Request 共享调用方的嵌套 Mapping。"""

    if not isinstance(spec, WorkflowCreateSpec):
        raise OrchestrationValidationError(
            "workflow must be a WorkflowCreateSpec"
        )
    copied_tasks = tuple(
        TaskCreateSpec(
            key=task.key,
            title=task.title,
            prompt=task.prompt,
            role=(
                task.role.strip()
                if type(task.role) is str
                else task.role
            ),
            depends_on=tuple(task.depends_on),
            priority=task.priority,
            max_attempts=task.max_attempts,
            workdir=task.workdir,
            input_metadata=plain_json_object(
                task.input_metadata,
                field_name="input_metadata",
            ),
        )
        if isinstance(task, TaskCreateSpec)
        else task
        for task in spec.tasks
    )
    return WorkflowCreateSpec(
        title=spec.title,
        goal=spec.goal,
        created_by_session=spec.created_by_session,
        tasks=copied_tasks,
    )


def _copy_agent_definitions(
    definitions: object,
) -> tuple[AgentRoleDefinition, ...]:
    """复制并校验单次调用的动态职责目录。"""

    if not isinstance(definitions, (list, tuple)):
        raise OrchestrationValidationError(
            "agents must be a sequence of AgentRoleDefinition values"
        )
    if not 1 <= len(definitions) <= _MAX_AGENT_DEFINITIONS:
        raise OrchestrationValidationError(
            "agent definition count exceeds its limit"
        )
    copied: list[AgentRoleDefinition] = []
    for definition in definitions:
        if not isinstance(definition, AgentRoleDefinition):
            raise OrchestrationValidationError(
                "agents must contain AgentRoleDefinition values"
            )
        try:
            copied.append(AgentRoleDefinition(
                name=definition.name,
                instructions=definition.instructions,
            ))
        except (TypeError, ValueError) as exc:
            raise OrchestrationValidationError(
                "agent definition is invalid"
            ) from exc
    names = tuple(definition.name for definition in copied)
    if len(names) != len(set(names)):
        raise OrchestrationValidationError(
            "agent definition names must be unique"
        )
    if sum(len(definition.instructions) for definition in copied) > (
        _MAX_TOTAL_AGENT_INSTRUCTIONS_CHARS
    ):
        raise OrchestrationValidationError(
            "agent instructions exceed the total size limit"
        )
    return tuple(copied)


@dataclass(frozen=True, slots=True)
class OrchestrationRunRequest:
    """一次创建并运行固定 DAG 的不可变应用请求。"""

    workflow: WorkflowCreateSpec
    agents: tuple[AgentRoleDefinition, ...]
    result_task_key: str | None
    max_concurrency: int

    def __post_init__(self) -> None:
        copied_workflow = _copy_workflow_spec(self.workflow)
        copied_agents = _copy_agent_definitions(self.agents)
        agent_names = frozenset(
            definition.name for definition in copied_agents
        )
        used_agent_names: set[str] = set()
        for task in copied_workflow.tasks:
            if not isinstance(task, TaskCreateSpec):
                raise OrchestrationValidationError(
                    "workflow tasks must be TaskCreateSpec values"
                )
            if type(task.role) is not str or not task.role:
                raise OrchestrationValidationError(
                    "task role must be a non-empty string"
                )
            if task.role not in agent_names:
                raise OrchestrationValidationError(
                    "task role must reference a defined agent"
                )
            used_agent_names.add(task.role)
        if used_agent_names != agent_names:
            raise OrchestrationValidationError(
                "every agent definition must be referenced by a task"
            )
        result_task_key = self.result_task_key
        if result_task_key is not None and (
            type(result_task_key) is not str or not result_task_key.strip()
        ):
            raise OrchestrationValidationError(
                "result_task_key must be a non-empty string or None"
            )
        if type(self.max_concurrency) is not int or self.max_concurrency <= 0:
            raise OrchestrationValidationError(
                "max_concurrency must be a positive integer"
            )
        if result_task_key is not None:
            matching_keys = sum(
                task.key == result_task_key
                for task in copied_workflow.tasks
                if isinstance(task, TaskCreateSpec)
            )
            if matching_keys != 1:
                raise OrchestrationValidationError(
                    "result_task_key must identify one workflow task"
                )
        object.__setattr__(self, "workflow", copied_workflow)
        object.__setattr__(self, "agents", copied_agents)


@dataclass(frozen=True, slots=True)
class OrchestrationTaskReport:
    """不含 Prompt、Session 或 claim 信息的稳定 Task 结果投影。"""

    task_id: str
    task_key: str
    title: str
    role: str
    status: TaskStatus
    result_summary: str | None
    result_metadata: Mapping[str, object] | None
    error_type: str | None
    error_message: str | None
    blocked_reason: str | None

    def __post_init__(self) -> None:
        for field_name in ("task_id", "task_key", "title", "role"):
            value = getattr(self, field_name)
            if type(value) is not str or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        try:
            status = TaskStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise ValueError("task report status is invalid") from exc
        for field_name in (
            "result_summary",
            "error_type",
            "error_message",
            "blocked_reason",
        ):
            value = getattr(self, field_name)
            if value is not None and type(value) is not str:
                raise TypeError(f"{field_name} must be a string or None")
        metadata = self.result_metadata
        if metadata is not None:
            metadata = freeze_json_object(
                metadata,
                field_name="result_metadata",
            )
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "result_metadata", metadata)

    @classmethod
    def from_record(cls, task: TaskRecord) -> "OrchestrationTaskReport":
        if not isinstance(task, TaskRecord):
            raise TypeError("task must be a TaskRecord")
        return cls(
            task_id=task.task_id,
            task_key=task.task_key,
            title=task.title,
            role=task.role,
            status=task.status,
            result_summary=task.result_summary,
            result_metadata=task.result_metadata,
            error_type=task.error_type,
            error_message=task.error_message,
            blocked_reason=task.blocked_reason,
        )


@dataclass(frozen=True, slots=True)
class OrchestrationRunReport:
    """一次应用调用的稳定报告；SQLite Snapshot 仍是事实来源。"""

    workflow_id: str
    execution_kind: WorkflowExecutionKind
    workflow_status: WorkflowStatus | None
    snapshot_fresh: bool
    result_task_key: str
    result_summary: str | None
    result_metadata: Mapping[str, object] | None
    scheduled_task_ids: tuple[str, ...]
    tasks: tuple[OrchestrationTaskReport, ...]
    error_type: str | None
    error_message: str | None

    def __post_init__(self) -> None:
        if type(self.workflow_id) is not str or not self.workflow_id:
            raise ValueError("workflow_id must be a non-empty string")
        if type(self.result_task_key) is not str or not self.result_task_key:
            raise ValueError("result_task_key must be a non-empty string")
        try:
            execution_kind = WorkflowExecutionKind(self.execution_kind)
        except (TypeError, ValueError) as exc:
            raise ValueError("execution_kind is invalid") from exc
        workflow_status = self.workflow_status
        if workflow_status is not None:
            try:
                workflow_status = WorkflowStatus(workflow_status)
            except (TypeError, ValueError) as exc:
                raise ValueError("workflow_status is invalid") from exc
        if type(self.snapshot_fresh) is not bool:
            raise TypeError("snapshot_fresh must be a boolean")
        if workflow_status is None and self.snapshot_fresh:
            raise ValueError("a missing workflow status cannot be fresh")
        scheduled_task_ids = tuple(self.scheduled_task_ids)
        if any(
            type(task_id) is not str or not task_id
            for task_id in scheduled_task_ids
        ):
            raise ValueError("scheduled_task_ids contains an invalid task_id")
        tasks = tuple(self.tasks)
        if any(not isinstance(task, OrchestrationTaskReport) for task in tasks):
            raise TypeError("tasks must contain OrchestrationTaskReport values")
        for field_name in (
            "result_summary",
            "error_type",
            "error_message",
        ):
            value = getattr(self, field_name)
            if value is not None and type(value) is not str:
                raise TypeError(f"{field_name} must be a string or None")
        metadata = self.result_metadata
        if metadata is not None:
            metadata = freeze_json_object(
                metadata,
                field_name="result_metadata",
            )
        object.__setattr__(self, "execution_kind", execution_kind)
        object.__setattr__(self, "workflow_status", workflow_status)
        object.__setattr__(self, "scheduled_task_ids", scheduled_task_ids)
        object.__setattr__(self, "tasks", tasks)
        object.__setattr__(self, "result_metadata", metadata)


class WorkflowRunnerFactory(Protocol):
    """为每次应用调用创建独立 WorkflowRunner 的端口。"""

    def create(
        self,
        *,
        max_concurrency: int,
        role_resolver: RoleResolver,
    ) -> WorkflowRunner:
        """返回不共享 runner_id、线程池或活动任务的新 Runner。"""


class OrchestrationApplication:
    """只通过领域 Service 和 Runner Factory 完成编排应用用例。"""

    __slots__ = (
        "_max_supported_concurrency",
        "_role_resolver_factory",
        "_runner_factory",
        "_service",
    )

    def __init__(
        self,
        *,
        service: OrchestrationService,
        role_resolver_factory: RoleResolverFactory,
        runner_factory: WorkflowRunnerFactory,
        max_supported_concurrency: int,
    ) -> None:
        if not isinstance(service, OrchestrationService):
            raise TypeError("service must be an OrchestrationService")
        if not callable(getattr(role_resolver_factory, "create", None)):
            raise TypeError("role_resolver_factory must provide create()")
        if not callable(getattr(runner_factory, "create", None)):
            raise TypeError("runner_factory must provide create()")
        if (
            type(max_supported_concurrency) is not int
            or not 1
            <= max_supported_concurrency
            <= _MAX_APPLICATION_CONCURRENCY
        ):
            raise ValueError(
                "max_supported_concurrency must be within its limit"
            )
        self._service = service
        self._role_resolver_factory = role_resolver_factory
        self._runner_factory = runner_factory
        self._max_supported_concurrency = max_supported_concurrency

    def run(
        self,
        request: OrchestrationRunRequest,
        *,
        cancel_checker: Callable[[], bool] | None = None,
        hook_registry: object | None = None,
        parent_run_id: str | None = None,
        tool_context: Mapping[str, object] | None = None,
    ) -> OrchestrationRunReport:
        """原子创建 Workflow，并用一次独立 Runner 同步推进固定 DAG。"""

        if not isinstance(request, OrchestrationRunRequest):
            raise OrchestrationValidationError(
                "request must be an OrchestrationRunRequest"
            )
        if request.max_concurrency > self._max_supported_concurrency:
            raise OrchestrationValidationError(
                "max_concurrency exceeds the application limit"
            )
        if cancel_checker is not None and not callable(cancel_checker):
            raise OrchestrationValidationError(
                "cancel_checker must be callable"
            )
        if tool_context is not None and not isinstance(tool_context, Mapping):
            raise OrchestrationValidationError(
                "tool_context must be a mapping"
            )

        result_task_key = self._resolve_result_task_key(request)
        role_resolver = self._create_role_resolver(request.agents)
        workflow = self._service.create_workflow(request.workflow)
        workflow_id = workflow.workflow_id
        try:
            runner = self._runner_factory.create(
                max_concurrency=request.max_concurrency,
                role_resolver=role_resolver,
            )
            if not callable(getattr(runner, "run", None)):
                raise WorkflowRunnerError(
                    "runner_factory returned an invalid workflow runner",
                    persistence_unknown=False,
                )
        except Exception as exc:
            raise OrchestrationRunCreatedError(
                workflow_id=workflow_id,
                result_task_key=result_task_key,
                error_type="orchestration_runner_creation_failed",
                safe_message=(
                    "workflow was created but its runner could not be created"
                ),
                persistence_unknown=False,
            ) from exc

        try:
            result = runner.run(
                workflow_id,
                cancel_checker=cancel_checker,
                hook_registry=hook_registry,
                parent_run_id=parent_run_id,
                tool_context=(
                    None if tool_context is None else dict(tool_context)
                ),
            )
        except OrchestrationPersistenceError as exc:
            raise OrchestrationRunCreatedError(
                workflow_id=workflow_id,
                result_task_key=result_task_key,
                error_type="orchestration_persistence_unknown",
                safe_message=(
                    "workflow was created but its persistence outcome is "
                    "unknown"
                ),
                persistence_unknown=True,
            ) from exc
        except WorkflowRunnerError as exc:
            if exc.error_type == "workflow_pool_close_failed":
                error_type = "orchestration_pool_close_failed"
                safe_message = (
                    "workflow was created but its execution pool lifecycle "
                    "could not be confirmed"
                )
            else:
                error_type = "orchestration_runner_failed"
                safe_message = (
                    "workflow was created but its runner did not produce a "
                    "stable result"
                )
            raise OrchestrationRunCreatedError(
                workflow_id=workflow_id,
                result_task_key=result_task_key,
                error_type=error_type,
                safe_message=safe_message,
                persistence_unknown=exc.persistence_unknown,
            ) from exc
        except Exception as exc:
            raise OrchestrationRunCreatedError(
                workflow_id=workflow_id,
                result_task_key=result_task_key,
                error_type="orchestration_runner_failed",
                safe_message=(
                    "workflow was created but its runner did not produce a "
                    "stable result"
                ),
                persistence_unknown=True,
            ) from exc

        if (
            not isinstance(result, WorkflowExecutionResult)
            or result.workflow_id != workflow_id
        ):
            cause = WorkflowRunnerError(
                "workflow runner returned an invalid execution result"
            )
            raise OrchestrationRunCreatedError(
                workflow_id=workflow_id,
                result_task_key=result_task_key,
                error_type="orchestration_runner_result_invalid",
                safe_message=(
                    "workflow was created but its runner returned an invalid "
                    "result"
                ),
                persistence_unknown=True,
            ) from cause
        try:
            return self._build_report(
                result,
                result_task_key=result_task_key,
            )
        except Exception as exc:
            raise OrchestrationRunCreatedError(
                workflow_id=workflow_id,
                result_task_key=result_task_key,
                error_type="orchestration_report_failed",
                safe_message=(
                    "workflow was created but its report could not be "
                    "constructed"
                ),
                persistence_unknown=(
                    result.kind
                    is WorkflowExecutionKind.PERSISTENCE_UNKNOWN
                ),
            ) from exc

    def _create_role_resolver(
        self,
        definitions: tuple[AgentRoleDefinition, ...],
    ) -> RoleResolver:
        """在写库前构造并核验本次 Workflow 的独立职责目录。"""

        try:
            resolver = self._role_resolver_factory.create(definitions)
            resolve = getattr(resolver, "resolve", None)
            if not callable(resolve):
                raise TypeError("role resolver must provide resolve()")
            for definition in definitions:
                role = resolve(definition.name)
                if (
                    not isinstance(role, AgentRoleSpec)
                    or role.name != definition.name
                    or role.system_prompt != definition.instructions
                ):
                    raise TypeError(
                        "role resolver returned an invalid role plan"
                    )
            return resolver
        except OrchestrationValidationError:
            raise
        except Exception as exc:
            raise OrchestrationValidationError(
                "agent role definitions could not be resolved"
            ) from exc

    @staticmethod
    def _resolve_result_task_key(
        request: OrchestrationRunRequest,
    ) -> str:
        tasks = request.workflow.tasks
        task_keys: list[str] = []
        for task in tasks:
            if not isinstance(task, TaskCreateSpec):
                raise OrchestrationValidationError(
                    "workflow tasks must be TaskCreateSpec values"
                )
            if type(task.key) is not str or not task.key.strip():
                raise OrchestrationValidationError(
                    "task key must be a non-empty string"
                )
            task_keys.append(task.key)
        if len(task_keys) != len(set(task_keys)):
            raise OrchestrationValidationError(
                "task keys must be unique within a workflow"
            )

        explicit = request.result_task_key
        if explicit is not None:
            if explicit not in set(task_keys):
                raise OrchestrationValidationError(
                    "result_task_key references an unknown task"
                )
            return explicit

        downstream_count = {key: 0 for key in task_keys}
        known_keys = set(task_keys)
        for task in tasks:
            for dependency_key in task.depends_on:
                if (
                    type(dependency_key) is not str
                    or not dependency_key.strip()
                ):
                    raise OrchestrationValidationError(
                        "task dependency key must be a non-empty string"
                    )
                if dependency_key not in known_keys:
                    raise OrchestrationValidationError(
                        "task dependency references an unknown task key"
                    )
                downstream_count[dependency_key] += 1
        sinks = tuple(
            key for key in task_keys if downstream_count[key] == 0
        )
        if len(sinks) != 1:
            raise OrchestrationValidationError(
                "result_task_key is required unless the workflow has one sink"
            )
        return sinks[0]

    @staticmethod
    def _build_report(
        result: WorkflowExecutionResult,
        *,
        result_task_key: str,
    ) -> OrchestrationRunReport:
        snapshot = result.snapshot
        if snapshot is None:
            return OrchestrationRunReport(
                workflow_id=result.workflow_id,
                execution_kind=result.kind,
                workflow_status=None,
                snapshot_fresh=False,
                result_task_key=result_task_key,
                result_summary=None,
                result_metadata=None,
                scheduled_task_ids=result.scheduled_task_ids,
                tasks=(),
                error_type=result.error_type,
                error_message=result.error_message,
            )

        task_reports = tuple(
            OrchestrationTaskReport.from_record(task)
            for task in snapshot.tasks
        )
        result_tasks = tuple(
            task for task in snapshot.tasks
            if task.task_key == result_task_key
        )
        if len(result_tasks) != 1:
            raise WorkflowRunnerError(
                "workflow snapshot does not contain the selected result task"
            )
        result_task = result_tasks[0]
        if (
            snapshot.workflow.status is WorkflowStatus.COMPLETED
            and result_task.status is not TaskStatus.COMPLETED
        ):
            raise WorkflowRunnerError(
                "completed workflow has an incomplete selected result task"
            )
        if result.kind is WorkflowExecutionKind.COMPLETED and (
            not result.snapshot_fresh
            or snapshot.workflow.status is not WorkflowStatus.COMPLETED
            or result_task.status is not TaskStatus.COMPLETED
        ):
            raise WorkflowRunnerError(
                "completed execution result lacks a fresh completed result task"
            )

        expose_result = (
            snapshot.workflow.status is WorkflowStatus.COMPLETED
            and result_task.status is TaskStatus.COMPLETED
        )
        return OrchestrationRunReport(
            workflow_id=result.workflow_id,
            execution_kind=result.kind,
            workflow_status=snapshot.workflow.status,
            snapshot_fresh=result.snapshot_fresh,
            result_task_key=result_task_key,
            result_summary=(
                result_task.result_summary if expose_result else None
            ),
            result_metadata=(
                result_task.result_metadata if expose_result else None
            ),
            scheduled_task_ids=result.scheduled_task_ids,
            tasks=task_reports,
            error_type=result.error_type,
            error_message=result.error_message,
        )


__all__ = [
    "OrchestrationApplication",
    "OrchestrationRunReport",
    "OrchestrationRunRequest",
    "OrchestrationTaskReport",
    "WorkflowRunnerFactory",
]
