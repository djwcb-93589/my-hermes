"""持久化任务编排的状态枚举与不可变领域数据。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from hermes.orchestration.errors import OrchestrationValidationError


class WorkflowStatus(StrEnum):
    """Workflow 的稳定状态集合。"""

    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    """Task 的稳定状态集合。"""

    TODO = "todo"
    READY = "ready"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskRunStatus(StrEnum):
    """一次 Task claim 对应 Run 的稳定状态集合。"""

    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


WORKFLOW_TERMINAL_STATUSES = frozenset({
    WorkflowStatus.COMPLETED,
    WorkflowStatus.FAILED,
    WorkflowStatus.CANCELLED,
})
TASK_TERMINAL_STATUSES = frozenset({
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
})
TASK_RUN_TERMINAL_STATUSES = frozenset({
    TaskRunStatus.COMPLETED,
    TaskRunStatus.FAILED,
    TaskRunStatus.BLOCKED,
    TaskRunStatus.CANCELLED,
    TaskRunStatus.ABANDONED,
})


def _freeze_json_value(value: object, *, field_name: str) -> object:
    """递归复制 JSON 数据，并拒绝任意 Python 对象进入领域记录。"""

    if value is None or type(value) in (bool, int):
        return value
    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise OrchestrationValidationError(
                f"{field_name} must contain valid Unicode strings"
            ) from exc
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise OrchestrationValidationError(
                f"{field_name} must contain finite JSON numbers"
            )
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise OrchestrationValidationError(
                    f"{field_name} must contain only string object keys"
                )
            try:
                key.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise OrchestrationValidationError(
                    f"{field_name} must contain valid Unicode object keys"
                ) from exc
            frozen[key] = _freeze_json_value(
                item,
                field_name=field_name,
            )
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json_value(item, field_name=field_name)
            for item in value
        )
    raise OrchestrationValidationError(
        f"{field_name} must contain only JSON-compatible data"
    )


def freeze_json_object(
    value: Mapping[str, object],
    *,
    field_name: str,
) -> Mapping[str, object]:
    """复制并冻结一个 JSON object。"""

    if not isinstance(value, Mapping):
        raise OrchestrationValidationError(
            f"{field_name} must be a JSON object"
        )
    frozen = _freeze_json_value(value, field_name=field_name)
    if not isinstance(frozen, Mapping):
        raise OrchestrationValidationError(
            f"{field_name} must be a JSON object"
        )
    return frozen


def _plain_json_value(value: object, *, field_name: str) -> object:
    """从领域冻结容器导出全新普通 JSON 数据。"""

    if value is None or type(value) in (bool, int):
        return value
    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise OrchestrationValidationError(
                f"{field_name} must contain valid Unicode strings"
            ) from exc
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise OrchestrationValidationError(
                f"{field_name} must contain finite JSON numbers"
            )
        return value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise OrchestrationValidationError(
                    f"{field_name} must contain only string object keys"
                )
            try:
                key.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise OrchestrationValidationError(
                    f"{field_name} must contain valid Unicode object keys"
                ) from exc
            result[key] = _plain_json_value(item, field_name=field_name)
        return result
    if isinstance(value, (list, tuple)):
        return [
            _plain_json_value(item, field_name=field_name)
            for item in value
        ]
    raise OrchestrationValidationError(
        f"{field_name} must contain only JSON-compatible data"
    )


def plain_json_object(
    value: Mapping[str, object],
    *,
    field_name: str,
) -> dict[str, object]:
    """导出独立普通 dict，供明确的 JSON 持久化边界使用。"""

    if not isinstance(value, Mapping):
        raise OrchestrationValidationError(
            f"{field_name} must be a JSON object"
        )
    plain = _plain_json_value(value, field_name=field_name)
    if not isinstance(plain, dict):
        raise OrchestrationValidationError(
            f"{field_name} must be a JSON object"
        )
    return plain


@dataclass(frozen=True, slots=True)
class TaskCreateSpec:
    """一次 Workflow 创建请求中的临时 Task 定义。"""

    key: str
    title: str
    prompt: str
    role: str
    depends_on: tuple[str, ...] = ()
    priority: int = 0
    max_attempts: int = 1
    workdir: str | None = None
    input_metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.depends_on, (list, tuple)):
            raise OrchestrationValidationError(
                "depends_on must be a sequence of task keys"
            )
        object.__setattr__(self, "depends_on", tuple(self.depends_on))
        object.__setattr__(
            self,
            "input_metadata",
            freeze_json_object(
                self.input_metadata,
                field_name="input_metadata",
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkflowCreateSpec:
    """原子创建 Workflow 与整张 Task DAG 的完整请求。"""

    title: str
    goal: str
    created_by_session: str | None
    tasks: tuple[TaskCreateSpec, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.tasks, (list, tuple)):
            raise OrchestrationValidationError(
                "tasks must be a sequence of TaskCreateSpec values"
            )
        object.__setattr__(self, "tasks", tuple(self.tasks))


@dataclass(frozen=True, slots=True)
class WorkflowRecord:
    """持久化 Workflow 的不可变快照。"""

    workflow_id: str
    title: str
    goal: str
    status: WorkflowStatus
    created_by_session: str | None
    created_at: float
    updated_at: float
    finished_at: float | None

    def __post_init__(self) -> None:
        try:
            normalized_status = WorkflowStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise OrchestrationValidationError(
                "workflow status is invalid"
            ) from exc
        object.__setattr__(self, "status", normalized_status)


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """持久化 Task 当前状态摘要的不可变快照。"""

    task_id: str
    workflow_id: str
    task_key: str
    title: str
    prompt: str
    role: str
    status: TaskStatus
    priority: int
    max_attempts: int
    # 已消耗的正式执行预算数：completed、failed、abandoned 消耗，
    # blocked、cancelled 不消耗。
    attempt_count: int
    workdir: str | None
    input_metadata: Mapping[str, object]
    claim_owner: str | None
    claim_token: str | None
    claim_expires_at: float | None
    result_summary: str | None
    result_metadata: Mapping[str, object] | None
    error_type: str | None
    error_message: str | None
    blocked_reason: str | None
    created_at: float
    ready_at: float | None
    started_at: float | None
    finished_at: float | None
    updated_at: float

    def __post_init__(self) -> None:
        try:
            normalized_status = TaskStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise OrchestrationValidationError(
                "task status is invalid"
            ) from exc
        object.__setattr__(self, "status", normalized_status)
        object.__setattr__(
            self,
            "input_metadata",
            freeze_json_object(
                self.input_metadata,
                field_name="input_metadata",
            ),
        )
        if self.result_metadata is not None:
            object.__setattr__(
                self,
                "result_metadata",
                freeze_json_object(
                    self.result_metadata,
                    field_name="result_metadata",
                ),
            )


@dataclass(frozen=True, slots=True)
class TaskRunRecord:
    """一次 Task claim 的持久化执行事实。"""

    run_id: str
    workflow_id: str
    task_id: str
    # Task 的单调递增 Run 序号，包含 block 后重领，不等于 attempt_count。
    attempt_number: int
    worker_id: str
    claim_token: str
    status: TaskRunStatus
    session_key: str | None
    claimed_at: float
    started_at: float | None
    heartbeat_at: float
    finished_at: float | None
    result_summary: str | None
    result_metadata: Mapping[str, object] | None
    error_type: str | None
    error_message: str | None

    def __post_init__(self) -> None:
        try:
            normalized_status = TaskRunStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise OrchestrationValidationError(
                "task run status is invalid"
            ) from exc
        object.__setattr__(self, "status", normalized_status)
        if self.result_metadata is not None:
            object.__setattr__(
                self,
                "result_metadata",
                freeze_json_object(
                    self.result_metadata,
                    field_name="result_metadata",
                ),
            )


@dataclass(frozen=True, slots=True)
class TaskClaim:
    """已经提交到 SQLite 的 Task、Run 与 fencing token 快照。"""

    workflow: WorkflowRecord
    task: TaskRecord
    run: TaskRunRecord
    claim_token: str
    claim_expires_at: float

    def __post_init__(self) -> None:
        if (
            self.task.task_id != self.run.task_id
            or self.task.workflow_id != self.workflow.workflow_id
            or self.run.workflow_id != self.workflow.workflow_id
            or self.task.status is not TaskStatus.RUNNING
            or self.run.status not in {
                TaskRunStatus.CLAIMED,
                TaskRunStatus.RUNNING,
            }
            or self.task.claim_token != self.claim_token
            or self.run.claim_token != self.claim_token
            or self.task.claim_expires_at != self.claim_expires_at
        ):
            raise OrchestrationValidationError(
                "task claim records do not describe one persisted claim"
            )


__all__ = [
    "TaskClaim",
    "TaskCreateSpec",
    "TaskRecord",
    "TaskRunRecord",
    "TaskRunStatus",
    "TaskStatus",
    "WorkflowCreateSpec",
    "WorkflowRecord",
    "WorkflowStatus",
]
