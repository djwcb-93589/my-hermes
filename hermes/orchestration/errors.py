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


__all__ = [
    "InvalidTaskTransitionError",
    "OrchestrationConflictError",
    "OrchestrationError",
    "OrchestrationNotFoundError",
    "OrchestrationPersistenceError",
    "OrchestrationValidationError",
    "TaskClaimLostError",
    "WorkflowCycleError",
]
