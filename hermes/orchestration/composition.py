"""把编排领域、隔离 Runtime 与具体基础设施显式组装为应用。"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Callable, Mapping
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
from hermes.orchestration.execution import (
    ClaimedTaskExecutor,
    ClaimedTaskExecutorFactory,
)
from hermes.orchestration.roles import (
    AgentRoleDefinition,
    AgentRoleSpec,
    RoleResolver,
    StaticRoleRegistry,
)
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


class FixedCapabilityRoleResolverFactory:
    """把单次动态职责补齐为固定权限和模型的不可变执行计划。"""

    __slots__ = (
        "_max_iterations",
        "_model",
        "_model_kwargs",
        "_toolsets",
    )

    def __init__(
        self,
        *,
        toolsets: tuple[str, ...],
        model: str,
        max_iterations: int,
        model_kwargs: Mapping[str, object],
    ) -> None:
        # 仅借助完整执行计划契约冻结固定能力，不形成可用业务角色。
        template = AgentRoleSpec(
            name="__fixed_capability_template__",
            system_prompt="Internal fixed capability validation template.",
            toolsets=toolsets,
            model=model,
            max_iterations=max_iterations,
            model_kwargs=model_kwargs,
        )
        self._toolsets = template.toolsets
        self._model = template.model
        self._max_iterations = template.max_iterations
        self._model_kwargs = template.model_kwargs

    def create(
        self,
        definitions: tuple[AgentRoleDefinition, ...],
    ) -> RoleResolver:
        if not isinstance(definitions, (list, tuple)) or not definitions:
            raise TypeError(
                "definitions must be a non-empty sequence"
            )
        roles: dict[str, AgentRoleSpec] = {}
        for definition in definitions:
            if not isinstance(definition, AgentRoleDefinition):
                raise TypeError(
                    "definitions must contain AgentRoleDefinition values"
                )
            if definition.name in roles:
                raise ValueError("agent definition names must be unique")
            roles[definition.name] = AgentRoleSpec(
                name=definition.name,
                system_prompt=definition.instructions,
                toolsets=self._toolsets,
                model=self._model,
                max_iterations=self._max_iterations,
                model_kwargs=self._model_kwargs,
            )
        return StaticRoleRegistry(roles)


class IsolatedAgentTaskExecutorFactory:
    """复用正式基础设施，为单次 Workflow 创建独立 Task Executor。"""

    __slots__ = (
        "_isolated_agent_executor",
        "_max_goal_chars",
        "_service",
        "_tool_resolver",
    )

    def __init__(
        self,
        *,
        service: OrchestrationService,
        tool_resolver: RegistryTaskToolResolver,
        isolated_agent_executor: IsolatedAgentExecutor,
        max_goal_chars: int,
    ) -> None:
        if not isinstance(service, OrchestrationService):
            raise TypeError("service must be an OrchestrationService")
        if not callable(getattr(tool_resolver, "resolve", None)):
            raise TypeError("tool_resolver must provide resolve()")
        if not callable(getattr(isolated_agent_executor, "execute", None)):
            raise TypeError("isolated_agent_executor must provide execute()")
        resolver_registry = getattr(tool_resolver, "registry_identity", None)
        executor_registry = getattr(isolated_agent_executor, "registry", None)
        if (
            resolver_registry is None
            or executor_registry is None
            or resolver_registry is not executor_registry
        ):
            raise ValueError(
                "tool resolver and isolated executor must share one registry"
            )
        if (
            type(max_goal_chars) is not int
            or not 1 <= max_goal_chars <= _HARD_MAX_GOAL_CHARS
        ):
            raise ValueError("max_goal_chars must be within its hard limit")
        self._service = service
        self._tool_resolver = tool_resolver
        self._isolated_agent_executor = isolated_agent_executor
        self._max_goal_chars = max_goal_chars

    def create(
        self,
        *,
        role_resolver: RoleResolver,
    ) -> ClaimedTaskExecutor:
        if not callable(getattr(role_resolver, "resolve", None)):
            raise TypeError("role_resolver must provide resolve()")
        return IsolatedAgentTaskExecutor(
            service=self._service,
            role_resolver=role_resolver,
            tool_resolver=self._tool_resolver,
            isolated_agent_executor=self._isolated_agent_executor,
            max_goal_chars=self._max_goal_chars,
        )


class CentralWorkflowRunnerFactory:
    """为每次应用调用创建全新中央 Runner 的具体 Factory。"""

    __slots__ = (
        "_pool_factory",
        "_runner_id_factory",
        "_service",
        "_settings",
        "_task_executor_factory",
    )

    def __init__(
        self,
        *,
        service: OrchestrationService,
        task_executor_factory: ClaimedTaskExecutorFactory,
        pool_factory: WorkflowTaskExecutionPoolFactory,
        settings: OrchestrationRuntimeSettings,
        runner_id_factory: Callable[[], object] = uuid.uuid4,
    ) -> None:
        if not isinstance(service, OrchestrationService):
            raise TypeError("service must be an OrchestrationService")
        if not callable(getattr(task_executor_factory, "create", None)):
            raise TypeError("task_executor_factory must provide create()")
        if not callable(getattr(pool_factory, "create", None)):
            raise TypeError("pool_factory must provide create()")
        if not isinstance(settings, OrchestrationRuntimeSettings):
            raise TypeError("settings must be OrchestrationRuntimeSettings")
        if not callable(runner_id_factory):
            raise TypeError("runner_id_factory must be callable")
        self._service = service
        self._task_executor_factory = task_executor_factory
        self._pool_factory = pool_factory
        self._settings = settings
        self._runner_id_factory = runner_id_factory

    def create(
        self,
        *,
        max_concurrency: int,
        role_resolver: RoleResolver,
    ) -> WorkflowRunner:
        if (
            type(max_concurrency) is not int
            or not 1
            <= max_concurrency
            <= self._settings.maximum_max_concurrency
        ):
            raise WorkflowRunnerValidationError(
                "max_concurrency exceeds the configured limit"
            )
        if not callable(getattr(role_resolver, "resolve", None)):
            raise WorkflowRunnerValidationError(
                "role_resolver must provide resolve()"
            )
        task_executor = self._task_executor_factory.create(
            role_resolver=role_resolver
        )
        if not callable(getattr(task_executor, "execute_claim", None)):
            raise WorkflowRunnerError(
                "task_executor_factory returned an invalid executor",
                persistence_unknown=False,
            )
        runner_id = self._new_runner_id()
        return CentralWorkflowRunner(
            service=self._service,
            task_executor=task_executor,
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
    role_resolver_factory = FixedCapabilityRoleResolverFactory(
        toolsets=_DEFAULT_ROLE_TOOLSETS,
        model=model,
        max_iterations=max_child_iterations,
        model_kwargs={"max_tokens": max_output_tokens},
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
    capability_validation_definition = AgentRoleDefinition(
        name="__capability_validation__",
        instructions="Validate fixed orchestration worker capabilities.",
    )
    capability_validation_roles = role_resolver_factory.create(
        (capability_validation_definition,)
    )
    validation_resolver.resolve(
        capability_validation_roles.resolve(
            capability_validation_definition.name
        )
    )
    isolated_executor = IsolatedAgentExecutor(
        registry=execution_registry,
        client=model_client,
        process_manager=process_manager,
    )
    task_executor_factory = IsolatedAgentTaskExecutorFactory(
        service=service,
        tool_resolver=tool_resolver,
        isolated_agent_executor=isolated_executor,
        max_goal_chars=active_settings.max_goal_chars,
    )
    pool_factory = ThreadedWorkflowTaskExecutionPoolFactory()
    runner_factory = CentralWorkflowRunnerFactory(
        service=service,
        task_executor_factory=task_executor_factory,
        pool_factory=pool_factory,
        settings=active_settings,
    )
    return OrchestrationApplication(
        service=service,
        role_resolver_factory=role_resolver_factory,
        runner_factory=runner_factory,
        max_supported_concurrency=(
            active_settings.maximum_max_concurrency
        ),
    )


__all__ = [
    "CentralWorkflowRunnerFactory",
    "FixedCapabilityRoleResolverFactory",
    "IsolatedAgentTaskExecutorFactory",
    "OrchestrationRuntimeSettings",
    "build_orchestration_application",
]
