"""单次已领取 Task 的纯执行契约，不绑定 Agent 或工具实现。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from hermes.orchestration.models import TaskClaim


if TYPE_CHECKING:
    from hermes.orchestration.roles import AgentRoleSpec


class TaskExecutionOutcomeKind(StrEnum):
    """同时说明 Agent 结果与持久化落点的一次执行结果。

    PERSISTENCE_UNKNOWN 仅表示已尝试状态变更，但无法确认事务是否提交。
    """

    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    RELEASED = "released"
    CLAIM_LOST = "claim_lost"
    PERSISTENCE_UNKNOWN = "persistence_unknown"


_PERSISTED_OUTCOME_KINDS = frozenset({
    TaskExecutionOutcomeKind.COMPLETED,
    TaskExecutionOutcomeKind.FAILED,
    TaskExecutionOutcomeKind.BLOCKED,
    TaskExecutionOutcomeKind.RELEASED,
})


def _freeze_value(value: object) -> object:
    """递归复制工具定义，避免解析边界在执行期间被修改。"""

    if isinstance(value, Mapping):
        return MappingProxyType({
            deepcopy(key): _freeze_value(item)
            for key, item in value.items()
        })
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_value(item) for item in value)
    return deepcopy(value)


def _freeze_definition(
    value: Mapping[str, object],
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("tool definitions must contain mappings")
    frozen = _freeze_value(value)
    if not isinstance(frozen, Mapping):
        raise TypeError("tool definition must remain a mapping")
    return frozen


def _definition_name(definition: Mapping[str, object]) -> str | None:
    function = definition.get("function")
    if not isinstance(function, Mapping):
        return None
    name = function.get("name")
    return name if type(name) is str and name else None


@dataclass(frozen=True, slots=True)
class TaskExecutionOutcome:
    """一次 Worker 调用报告；SQLite 记录仍是任务状态的唯一真相。"""

    kind: TaskExecutionOutcomeKind
    workflow_id: str
    task_id: str
    run_id: str
    session_key: str | None
    runtime_status: str | None
    summary: str | None
    error_type: str | None
    error_message: str | None
    retryable: bool
    persisted: bool

    def __post_init__(self) -> None:
        try:
            normalized_kind = TaskExecutionOutcomeKind(self.kind)
        except (TypeError, ValueError) as exc:
            raise ValueError("task execution outcome kind is invalid") from exc
        for field_name in ("workflow_id", "task_id", "run_id"):
            value = getattr(self, field_name)
            if type(value) is not str or not value:
                raise ValueError(f"{field_name} must be a non-empty string")
        for field_name in (
            "session_key",
            "runtime_status",
            "summary",
            "error_type",
            "error_message",
        ):
            value = getattr(self, field_name)
            if value is not None and type(value) is not str:
                raise TypeError(f"{field_name} must be a string or None")
        if type(self.retryable) is not bool or type(self.persisted) is not bool:
            raise TypeError("retryable and persisted must be booleans")
        if self.persisted != (normalized_kind in _PERSISTED_OUTCOME_KINDS):
            raise ValueError("persisted does not match task execution outcome kind")
        object.__setattr__(self, "kind", normalized_kind)


@dataclass(frozen=True, slots=True)
class ResolvedAgentTools:
    """同一次策略解析得到的模型 Schema 与 dispatch 名称边界。"""

    definitions: tuple[Mapping[str, object], ...]
    allowed_tool_names: frozenset[str]
    resolved_toolsets: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.definitions, (list, tuple)):
            raise TypeError("definitions must be a sequence")
        if not isinstance(self.allowed_tool_names, (set, frozenset)):
            raise TypeError("allowed_tool_names must be a set")
        if not isinstance(self.resolved_toolsets, (list, tuple)):
            raise TypeError("resolved_toolsets must be a sequence")
        definitions = tuple(
            _freeze_definition(definition)
            for definition in self.definitions
        )
        allowed_names = frozenset(self.allowed_tool_names)
        resolved_toolsets = tuple(self.resolved_toolsets)
        definition_names = tuple(
            _definition_name(definition)
            for definition in definitions
        )
        if (
            not definitions
            or any(name is None for name in definition_names)
            or len(definition_names) != len(set(definition_names))
        ):
            raise ValueError("definitions must contain unique function tools")
        if (
            not allowed_names
            or any(type(name) is not str or not name for name in allowed_names)
            or frozenset(definition_names) != allowed_names
        ):
            raise ValueError(
                "definitions and allowed_tool_names must describe one boundary"
            )
        if (
            not resolved_toolsets
            or any(
                type(toolset) is not str or not toolset
                for toolset in resolved_toolsets
            )
            or len(resolved_toolsets) != len(set(resolved_toolsets))
        ):
            raise ValueError(
                "resolved_toolsets must be non-empty and contain no duplicates"
            )
        object.__setattr__(self, "definitions", definitions)
        object.__setattr__(self, "allowed_tool_names", allowed_names)
        object.__setattr__(self, "resolved_toolsets", resolved_toolsets)


class TaskToolResolver(Protocol):
    """把 Role 的工具集请求解析为一次不可变工具能力边界。"""

    @property
    def registry_identity(self) -> object:
        """返回供 Worker 做对象同一性检查的不透明 Registry 身份。"""

    def resolve(self, role: AgentRoleSpec) -> ResolvedAgentTools:
        """解析角色工具；不可用或被完整过滤的工具集必须失败。"""


class TaskSessionSetupPlan(Protocol):
    """在 Runtime 接管 Session 后执行的独立初始化计划。"""

    def prepare(self) -> None:
        """初始化可被统一清理的资源；不得自行承担最终清理。"""


class TaskSessionPreparer(Protocol):
    """在状态写入前生成无资源副作用的 Session 初始化计划。"""

    def plan(
        self,
        *,
        session_key: str,
        workdir: str | None,
    ) -> TaskSessionSetupPlan:
        """只校验并冻结参数，不创建 Backend、目录、文件或进程。"""


class ClaimedTaskExecutor(Protocol):
    """同步执行一个已经持久化领取的 Task，不负责 claim 或调度。"""

    def execute_claim(
        self,
        claim: TaskClaim,
        *,
        cancel_checker: Callable[[], bool] | None = None,
        hook_registry: object | None = None,
        parent_run_id: str | None = None,
        tool_context: Mapping[str, object] | None = None,
    ) -> TaskExecutionOutcome:
        """执行一次 Claim；调用方负责在执行期间维持租约。"""


__all__ = [
    "ClaimedTaskExecutor",
    "ResolvedAgentTools",
    "TaskExecutionOutcome",
    "TaskExecutionOutcomeKind",
    "TaskSessionPreparer",
    "TaskSessionSetupPlan",
    "TaskToolResolver",
]
