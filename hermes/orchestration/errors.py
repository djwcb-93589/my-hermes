"""多 Agent 任务编排领域的稳定错误边界。"""

from __future__ import annotations


class OrchestrationError(Exception):
    """编排领域错误基类。"""


class OrchestrationValidationError(OrchestrationError):
    """调用参数或领域定义不满足约束。"""


class OrchestrationNotFoundError(OrchestrationError):
    """请求的 Workflow、Task 或 Run 不存在。"""


class OrchestrationConflictError(OrchestrationError):
    """当前持久化状态与请求操作冲突。"""


class InvalidTaskTransitionError(OrchestrationConflictError):
    """Workflow、Task 或 Run 不允许执行请求的状态转换。"""


class TaskClaimLostError(OrchestrationConflictError):
    """调用方持有的 claim token 已失效或不再属于当前 Task。"""


class WorkflowCycleError(OrchestrationValidationError):
    """Workflow 的 Task 依赖图包含环。"""


class OrchestrationPersistenceError(OrchestrationError):
    """SQLite 编排事实不可读、不可写或不满足持久化约束。"""


class OrchestrationRunError(OrchestrationError):
    """一次编排应用调用的稳定、安全失败契约。"""

    __slots__ = (
        "_error_type",
        "_persistence_unknown",
        "_safe_message",
    )

    def __init__(
        self,
        *,
        error_type: str,
        safe_message: str,
        persistence_unknown: bool,
    ) -> None:
        if (
            type(error_type) is not str
            or not error_type.strip()
            or len(error_type) > 256
        ):
            raise ValueError("error_type must be a non-empty bounded string")
        if (
            type(safe_message) is not str
            or not safe_message.strip()
            or len(safe_message) > 1_000
        ):
            raise ValueError(
                "safe_message must be a non-empty bounded string"
            )
        if type(persistence_unknown) is not bool:
            raise TypeError("persistence_unknown must be a boolean")
        super().__init__(safe_message)
        self._error_type = error_type
        self._safe_message = safe_message
        self._persistence_unknown = persistence_unknown

    @property
    def error_type(self) -> str:
        return self._error_type

    @property
    def safe_message(self) -> str:
        return self._safe_message

    @property
    def persistence_unknown(self) -> bool:
        return self._persistence_unknown


class OrchestrationRunCreatedError(OrchestrationRunError):
    """Workflow 已持久化后发生的稳定应用失败。"""

    __slots__ = ("_result_task_key", "_workflow_id")

    def __init__(
        self,
        *,
        workflow_id: str,
        error_type: str,
        safe_message: str,
        persistence_unknown: bool,
        result_task_key: str | None = None,
    ) -> None:
        if (
            type(workflow_id) is not str
            or not workflow_id.strip()
            or len(workflow_id) > 128
        ):
            raise ValueError("workflow_id must be a non-empty bounded string")
        if result_task_key is not None and (
            type(result_task_key) is not str
            or not result_task_key.strip()
            or len(result_task_key) > 128
        ):
            raise ValueError(
                "result_task_key must be a non-empty bounded string or None"
            )
        super().__init__(
            error_type=error_type,
            safe_message=safe_message,
            persistence_unknown=persistence_unknown,
        )
        self._workflow_id = workflow_id
        self._result_task_key = result_task_key

    @property
    def workflow_id(self) -> str:
        return self._workflow_id

    @property
    def result_task_key(self) -> str | None:
        return self._result_task_key


class WorkflowRunnerError(OrchestrationError):
    """Workflow Runner 无法安全完成调用或构造稳定结果。"""

    __slots__ = ("_runner_error_type", "_runner_persistence_unknown")

    default_error_type = "workflow_runner_error"
    default_persistence_unknown = True

    def __init__(
        self,
        message: str,
        *,
        error_type: str | None = None,
        persistence_unknown: bool | None = None,
    ) -> None:
        if type(message) is not str or not message:
            raise ValueError("workflow runner error message must be non-empty")
        resolved_error_type = (
            self.default_error_type if error_type is None else error_type
        )
        if (
            type(resolved_error_type) is not str
            or not resolved_error_type.strip()
            or len(resolved_error_type) > 256
        ):
            raise ValueError("workflow runner error_type is invalid")
        if persistence_unknown is not None and type(
            persistence_unknown
        ) is not bool:
            raise TypeError(
                "workflow runner persistence_unknown must be a boolean"
            )
        super().__init__(message)
        self._runner_error_type = resolved_error_type
        self._runner_persistence_unknown = (
            self.default_persistence_unknown
            if persistence_unknown is None
            else persistence_unknown
        )

    @property
    def error_type(self) -> str:
        return self._runner_error_type

    @property
    def persistence_unknown(self) -> bool:
        return self._runner_persistence_unknown


class WorkflowRunnerValidationError(WorkflowRunnerError):
    """Workflow Runner 的构造参数或运行参数无效。"""

    default_error_type = "workflow_runner_validation_error"
    default_persistence_unknown = False


class WorkflowTaskSubmissionError(WorkflowRunnerError):
    """Pool 提交失败，并明确报告 Worker 接收状态。"""

    def __init__(
        self,
        safe_message: str,
        *,
        accepted: bool | None,
    ) -> None:
        if (
            type(safe_message) is not str
            or not safe_message.strip()
            or len(safe_message) > 1_000
        ):
            raise ValueError("safe_message must be a non-empty bounded string")
        if accepted is not None and type(accepted) is not bool:
            raise TypeError("accepted must be a boolean or None")
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.accepted = accepted


class TaskExecutionError(OrchestrationError):
    """单任务执行准备阶段的稳定错误。"""

    default_error_type = "task_execution_preparation_failed"
    default_retryable = False

    def __init__(
        self,
        message: str,
        *,
        error_type: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        if type(message) is not str or not message:
            raise ValueError("task execution error message must be non-empty")
        resolved_error_type = (
            self.default_error_type if error_type is None else error_type
        )
        if (
            type(resolved_error_type) is not str
            or not resolved_error_type.strip()
            or len(resolved_error_type) > 256
        ):
            raise ValueError("task execution error_type is invalid")
        if retryable is not None and type(retryable) is not bool:
            raise TypeError("task execution retryable must be a boolean")
        super().__init__(message)
        self.error_type = resolved_error_type
        self.retryable = (
            self.default_retryable if retryable is None else retryable
        )


class UnknownAgentRoleError(TaskExecutionError):
    """任务声明的 Agent Role 未注册。"""

    default_error_type = "unknown_agent_role"


class TaskToolResolutionError(TaskExecutionError):
    """角色请求的工具能力无法形成完整安全边界。"""

    default_error_type = "task_tool_resolution_failed"


class TaskContextError(TaskExecutionError):
    """任务或直接依赖无法构造成受限、确定性的执行上下文。"""

    default_error_type = "task_context_invalid"


class TaskSessionPreparationError(TaskExecutionError):
    """独立 TaskRun Session 无法安全准备。"""

    default_error_type = "task_session_preparation_failed"


__all__ = [
    "InvalidTaskTransitionError",
    "OrchestrationConflictError",
    "OrchestrationError",
    "OrchestrationNotFoundError",
    "OrchestrationPersistenceError",
    "OrchestrationRunCreatedError",
    "OrchestrationRunError",
    "OrchestrationValidationError",
    "TaskClaimLostError",
    "TaskContextError",
    "TaskExecutionError",
    "TaskSessionPreparationError",
    "TaskToolResolutionError",
    "UnknownAgentRoleError",
    "WorkflowCycleError",
    "WorkflowRunnerError",
    "WorkflowRunnerValidationError",
    "WorkflowTaskSubmissionError",
]
