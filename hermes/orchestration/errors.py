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
    "OrchestrationValidationError",
    "TaskClaimLostError",
    "TaskContextError",
    "TaskExecutionError",
    "TaskSessionPreparationError",
    "TaskToolResolutionError",
    "UnknownAgentRoleError",
    "WorkflowCycleError",
]
