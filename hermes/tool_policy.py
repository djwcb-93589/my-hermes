"""工具声明与运行时注册共同使用的稳定策略定义。"""

from __future__ import annotations

from enum import Enum


class ExecutionEnvironment(str, Enum):
    """工具可被解析到的运行入口。"""

    CLI = "cli"
    GATEWAY = "gateway"
    CRON = "cron"
    DELEGATE = "delegate"
    BACKGROUND_REVIEW = "background_review"


class ApprovalMode(str, Enum):
    """工具在运行时可能采用的审批方式。"""

    NONE = "none"
    INTERACTIVE_OR_REMOTE = "interactive_or_remote"
    REMOTE_ONCE = "remote_once"


class ToolRiskLevel(str, Enum):
    """工具在声明层面的最高副作用风险。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


_TOOL_RISK_RANK = {
    ToolRiskLevel.LOW: 1,
    ToolRiskLevel.MEDIUM: 2,
    ToolRiskLevel.HIGH: 3,
}


def normalize_execution_environment(
    value: ExecutionEnvironment | str,
) -> ExecutionEnvironment:
    """把运行入口规范化为共享枚举。"""
    if isinstance(value, ExecutionEnvironment):
        return value
    return ExecutionEnvironment(str(value).lower())


def normalize_approval_mode(value: ApprovalMode | str) -> ApprovalMode:
    """把审批模式规范化为共享枚举。"""
    if isinstance(value, ApprovalMode):
        return value
    return ApprovalMode(str(value))


def normalize_tool_risk_level(
    value: ToolRiskLevel | str,
) -> ToolRiskLevel:
    """把风险等级规范化为共享枚举。"""
    if isinstance(value, ToolRiskLevel):
        return value
    return ToolRiskLevel(str(value))


def tool_risk_rank(value: ToolRiskLevel | str) -> int:
    """返回共享风险等级的稳定排序值。"""
    return _TOOL_RISK_RANK[normalize_tool_risk_level(value)]


__all__ = [
    "ApprovalMode",
    "ExecutionEnvironment",
    "ToolRiskLevel",
    "normalize_approval_mode",
    "normalize_execution_environment",
    "normalize_tool_risk_level",
    "tool_risk_rank",
]
