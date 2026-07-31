"""把编排领域、隔离 Runtime 与具体基础设施显式组装为应用。"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from hermes.orchestration.agent_worker import (
    IsolatedAgentTaskExecutor,
    RegistryTaskToolResolver,
)
from hermes.orchestration.application import OrchestrationApplication
from hermes.orchestration.central_runner import CentralWorkflowRunner
from hermes.orchestration.errors import (
    WorkflowRunnerError,
    WorkflowRunnerValidationError,
)
from hermes.orchestration.execution import ClaimedTaskExecutor
from hermes.orchestration.roles import AgentRoleSpec, StaticRoleRegistry
from hermes.orchestration.service import OrchestrationService
from hermes.orchestration.threaded_execution import (
    ThreadedWorkflowTaskExecutionPoolFactory,
)
from hermes.orchestration.workflow_execution import (
    WorkflowRunner,
    WorkflowTaskExecutionPoolFactory,
)
from hermes.persistence.orchestration import SQLiteOrchestrationStore
from hermes.subagents import IsolatedAgentExecutor
from hermes.tool_policy import ExecutionEnvironment, ToolRiskLevel
from hermes.tools import ToolRegistry


_HARD_MAX_CONCURRENCY = 8
_HARD_MAX_TASKS = 32
_HARD_MAX_STEPS = 10_000
_HARD_MAX_GOAL_CHARS = 1_000_000
_HARD_MAX_LEASE_SECONDS = 86_400.0
_HARD_MAX_POLL_SECONDS = 60.0
_MAX_RUNNER_ID_COMPONENT_LENGTH = 128
_SAFE_RUNNER_ID_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")
_DEFAULT_ROLE_TOOLSETS = ("skill_read",)

_DEFAULT_ROLE_PROMPTS = {
    "researcher": (
        "Collect relevant facts and documentary evidence for the assigned "
        "task. Separate observations from inference, identify uncertainty, "
        "and hand off a concise structured summary. Do not make the final "
        "workflow decision."
    ),
    "engineer": (
        "Perform the assigned technical implementation analysis. Be precise "
        "about code-level constraints and clearly state when the available "
        "read-only capabilities are insufficient. Do not claim edits or "
        "commands that were not actually performed."
    ),
    "reviewer": (
        "Independently review the direct upstream work. Identify errors, "
        "omissions, unsafe assumptions, compatibility risks, and missing "
        "evidence. Do not accept upstream conclusions by default."
    ),
    "synthesizer": (
        "Synthesize only the persisted direct-upstream results into one "
        "coherent final answer. Preserve material uncertainty, do not invent "
        "missing work, and do not create or delegate to another agent."
    ),
}


def _positive_finite_number(
    value: object,
    field_name: str,
    *,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite positive number")
    normalized = float(value)
    if (
        not math.isfinite(normalized)
        or normalized <= 0
        or normalized > maximum
    ):
        raise ValueError(
            f"{field_name} must be a finite positive number within its limit"
        )
    return normalized


@dataclass(frozen=True, slots=True)
class OrchestrationRuntimeSettings:
    """第一版同步编排的不可变、保守运行边界。"""

    default_max_concurrency: int = 3
    maximum_max_concurrency: int = 8
    lease_seconds: float = 300.0
    renew_interval_seconds: float = 60.0
    poll_interval_seconds: float = 0.1
    max_steps: int = 1_000
    max_goal_chars: int = 200_000
    max_tasks_per_workflow: int = 32

    def __post_init__(self) -> None:
        if (
            type(self.maximum_max_concurrency) is not int
            or not 1
            <= self.maximum_max_concurrency
            <= _HARD_MAX_CONCURRENCY
        ):
            raise ValueError(
                "maximum_max_concurrency must be within its hard limit"
            )
        if (
            type(self.default_max_concurrency) is not int
            or not 1
            <= self.default_max_concurrency
            <= self.maximum_max_concurrency
        ):
            raise ValueError(
                "default_max_concurrency must not exceed the maximum"
            )
        lease_seconds = _positive_finite_number(
            self.lease_seconds,
            "lease_seconds",
            maximum=_HARD_MAX_LEASE_SECONDS,
        )
        renew_interval_seconds = _positive_finite_number(
            self.renew_interval_seconds,
            "renew_interval_seconds",
            maximum=_HARD_MAX_LEASE_SECONDS,
        )
        if renew_interval_seconds >= lease_seconds:
            raise ValueError(
                "renew_interval_seconds must be less than lease_seconds"
            )
        poll_interval_seconds = _positive_finite_number(
            self.poll_interval_seconds,
            "poll_interval_seconds",
            maximum=_HARD_MAX_POLL_SECONDS,
        )
        if type(self.max_steps) is not int or not (
            1 <= self.max_steps <= _HARD_MAX_STEPS
        ):
            raise ValueError("max_steps must be within its hard limit")
        if type(self.max_goal_chars) is not int or not (
            1 <= self.max_goal_chars <= _HARD_MAX_GOAL_CHARS
        ):
            raise ValueError("max_goal_chars must be within its hard limit")
        if type(self.max_tasks_per_workflow) is not int or not (
            1 <= self.max_tasks_per_workflow <= _HARD_MAX_TASKS
        ):
            raise ValueError(
                "max_tasks_per_workflow must be within its hard limit"
            )
        object.__setattr__(self, "lease_seconds", lease_seconds)
        object.__setattr__(
            self,
            "renew_interval_seconds",
            renew_interval_seconds,
        )
        object.__setattr__(
            self,
            "poll_interval_seconds",
            poll_interval_seconds,
        )


class CentralWorkflowRunnerFactory:
    """为每次应用调用创建全新中央 Runner 的具体 Factory。"""

    __slots__ = (
        "_pool_factory",
        "_runner_id_factory",
        "_service",
        "_settings",
        "_task_executor",
    )

    def __init__(
        self,
        *,
        service: OrchestrationService,
        task_executor: ClaimedTaskExecutor,
        pool_factory: WorkflowTaskExecutionPoolFactory,
        settings: OrchestrationRuntimeSettings,
        runner_id_factory: Callable[[], object] = uuid.uuid4,
    ) -> None:
        if not isinstance(service, OrchestrationService):
            raise TypeError("service must be an OrchestrationService")
        if not callable(getattr(task_executor, "execute_claim", None)):
            raise TypeError("task_executor must provide execute_claim()")
        if not callable(getattr(pool_factory, "create", None)):
            raise TypeError("pool_factory must provide create()")
        if not isinstance(settings, OrchestrationRuntimeSettings):
            raise TypeError("settings must be OrchestrationRuntimeSettings")
        if not callable(runner_id_factory):
            raise TypeError("runner_id_factory must be callable")
        self._service = service
        self._task_executor = task_executor
        self._pool_factory = pool_factory
        self._settings = settings
        self._runner_id_factory = runner_id_factory

    def create(self, *, max_concurrency: int) -> WorkflowRunner:
        if (
            type(max_concurrency) is not int
            or not 1
            <= max_concurrency
            <= self._settings.maximum_max_concurrency
        ):
            raise WorkflowRunnerValidationError(
                "max_concurrency exceeds the configured limit"
            )
        runner_id = self._new_runner_id()
        return CentralWorkflowRunner(
            service=self._service,
            task_executor=self._task_executor,
            pool_factory=self._pool_factory,
            runner_id=runner_id,
            max_concurrency=max_concurrency,
            lease_seconds=self._settings.lease_seconds,
            renew_interval_seconds=self._settings.renew_interval_seconds,
            poll_interval_seconds=self._settings.poll_interval_seconds,
            max_steps=self._settings.max_steps,
        )

    def _new_runner_id(self) -> str:
        try:
            component = str(self._runner_id_factory()).strip()
        except Exception as exc:
            raise WorkflowRunnerError("runner_id factory failed") from exc
        if (
            not component
            or len(component) > _MAX_RUNNER_ID_COMPONENT_LENGTH
            or _SAFE_RUNNER_ID_COMPONENT.fullmatch(component) is None
        ):
            raise WorkflowRunnerError(
                "runner_id factory returned an invalid identifier"
            )
        return f"runner_{component}"


def _build_default_role_registry(
    *,
    model: str,
    max_output_tokens: int,
    max_child_iterations: int,
) -> StaticRoleRegistry:
    """组装不含 Delegate、写入或审批能力的四个静态角色。"""

    roles = {
        name: AgentRoleSpec(
            name=name,
            system_prompt=prompt,
            toolsets=_DEFAULT_ROLE_TOOLSETS,
            model=model,
            max_iterations=max_child_iterations,
            model_kwargs={"max_tokens": max_output_tokens},
        )
        for name, prompt in _DEFAULT_ROLE_PROMPTS.items()
    }
    return StaticRoleRegistry(roles)


def build_orchestration_application(
    *,
    db_path,
    execution_registry: ToolRegistry,
    process_manager,
    model_client,
    model: str,
    max_output_tokens: int,
    max_child_iterations: int,
    settings: OrchestrationRuntimeSettings | None = None,
    capability_validation_registry: ToolRegistry | None = None,
) -> OrchestrationApplication:
    """显式组装一次应用；不运行 Workflow、不建线程也不修改 Registry。"""

    active_settings = (
        OrchestrationRuntimeSettings() if settings is None else settings
    )
    if not isinstance(active_settings, OrchestrationRuntimeSettings):
        raise TypeError("settings must be OrchestrationRuntimeSettings")
    if not isinstance(execution_registry, ToolRegistry):
        raise TypeError("execution_registry must be a ToolRegistry")
    if (
        capability_validation_registry is not None
        and not isinstance(capability_validation_registry, ToolRegistry)
    ):
        raise TypeError(
            "capability_validation_registry must be a ToolRegistry or None"
        )
    if not callable(getattr(process_manager, "cleanup_session", None)):
        raise TypeError("process_manager must provide cleanup_session()")
    if model_client is None:
        raise TypeError("model_client is required")
    if type(model) is not str or not model.strip():
        raise ValueError("model must be a non-empty string")
    if type(max_output_tokens) is not int or max_output_tokens <= 0:
        raise ValueError("max_output_tokens must be a positive integer")
    if (
        type(max_child_iterations) is not int
        or not 1 <= max_child_iterations <= 1_000
    ):
        raise ValueError(
            "max_child_iterations must be a positive integer within its limit"
        )

    store = SQLiteOrchestrationStore(db_path)
    service = OrchestrationService(store)
    role_registry = _build_default_role_registry(
        model=model,
        max_output_tokens=max_output_tokens,
        max_child_iterations=max_child_iterations,
    )
    tool_resolver = RegistryTaskToolResolver(
        registry=execution_registry,
        environment=ExecutionEnvironment.DELEGATE,
        max_risk_level=ToolRiskLevel.LOW,
    )
    validation_resolver = RegistryTaskToolResolver(
        registry=(
            execution_registry
            if capability_validation_registry is None
            else capability_validation_registry
        ),
        environment=ExecutionEnvironment.DELEGATE,
        max_risk_level=ToolRiskLevel.LOW,
    )
    # staging 只在注册期验证声明；真正 Worker 始终使用正式 execution Registry。
    for role_name in _DEFAULT_ROLE_PROMPTS:
        validation_resolver.resolve(role_registry.resolve(role_name))
    isolated_executor = IsolatedAgentExecutor(
        registry=execution_registry,
        client=model_client,
        process_manager=process_manager,
    )
    task_executor = IsolatedAgentTaskExecutor(
        service=service,
        role_resolver=role_registry,
        tool_resolver=tool_resolver,
        isolated_agent_executor=isolated_executor,
        max_goal_chars=active_settings.max_goal_chars,
    )
    pool_factory = ThreadedWorkflowTaskExecutionPoolFactory()
    runner_factory = CentralWorkflowRunnerFactory(
        service=service,
        task_executor=task_executor,
        pool_factory=pool_factory,
        settings=active_settings,
    )
    return OrchestrationApplication(
        service=service,
        runner_factory=runner_factory,
        max_supported_concurrency=(
            active_settings.maximum_max_concurrency
        ),
    )


__all__ = [
    "CentralWorkflowRunnerFactory",
    "OrchestrationRuntimeSettings",
    "build_orchestration_application",
]
