"""Computer Use 的统一异常类型。"""

from collections.abc import Mapping
from typing import Any


class ComputerUseError(Exception):
    """所有 Computer Use 异常的基类。"""

    code: str = "computer_use_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """保存人类可读消息、稳定代码和结构化详情。"""

        super().__init__(message)
        self.message: str = message
        self.code: str = code if code is not None else type(self).code
        self.details: dict[str, Any] = (
            dict(details) if details is not None else {}
        )

    def __str__(self) -> str:
        """返回人类可读的错误消息。"""

        return self.message


class BackendUnavailableError(ComputerUseError):
    """Backend 不可用或尚未启动。"""

    code = "backend_unavailable"


class BackendStartError(ComputerUseError):
    """Backend 启动失败。"""

    code = "backend_start_failed"


class BackendDisconnectedError(ComputerUseError):
    """Backend 与底层驱动的连接已断开。"""

    code = "backend_disconnected"


class ActionTimeoutError(ComputerUseError):
    """动作未能在规定时间内完成。"""

    code = "action_timeout"


class InvalidArgumentsError(ComputerUseError):
    """动作参数无效或缺少必要组合。"""

    code = "invalid_arguments"


class TargetNotFoundError(ComputerUseError):
    """目标应用、窗口或界面元素不存在。"""

    code = "target_not_found"


class StaleElementError(ComputerUseError):
    """目标界面元素已经过期。"""

    code = "stale_element"


class PermissionDeniedError(ComputerUseError):
    """操作系统或目标应用拒绝了权限。"""

    code = "permission_denied"


class SafetyBlockedError(ComputerUseError):
    """动作被安全策略阻止。"""

    code = "safety_blocked"


class ApprovalDeniedError(ComputerUseError):
    """动作未获得所需批准。"""

    code = "approval_denied"


class ActionUnverifiedError(ComputerUseError):
    """动作已调用但无法确认真实生效。"""

    code = "action_unverified"


class ProtocolError(ComputerUseError):
    """cua-driver 返回了无效的 JSON-RPC 或 MCP 数据。"""

    code = "protocol_error"


class DriverNotFoundError(ComputerUseError):
    """系统无法找到配置的 cua-driver 命令。"""

    code = "driver_not_found"
