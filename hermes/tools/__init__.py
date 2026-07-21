"""全局工具注册表与按运行策略解析工具能力。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable


class ExecutionEnvironment(str, Enum):
    """工具可被解析到的运行入口。"""

    CLI = "cli"
    GATEWAY = "gateway"
    CRON = "cron"
    DELEGATE = "delegate"


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


def _normalize_environment(
    value: ExecutionEnvironment | str,
) -> ExecutionEnvironment:
    """兼容枚举值和外部传入的小写环境名称。"""
    if isinstance(value, ExecutionEnvironment):
        return value
    return ExecutionEnvironment(str(value).lower())


@dataclass(frozen=True)
class ToolPolicy:
    """一次会话解析工具时使用的运行策略。"""

    environment: ExecutionEnvironment | str
    enabled_toolsets: frozenset[str] | None = None
    unattended: bool = False
    trusted_context: frozenset[str] = field(default_factory=frozenset)
    allowed_approval_modes: frozenset[str] | None = None
    max_risk_level: ToolRiskLevel | str | None = None

    def __post_init__(self) -> None:
        """规范化外部入口传入的集合与环境名称。"""
        environment = _normalize_environment(self.environment)
        object.__setattr__(self, "environment", environment)
        if self.enabled_toolsets is not None:
            object.__setattr__(
                self,
                "enabled_toolsets",
                frozenset(str(item).strip().lower() for item in self.enabled_toolsets),
            )
        object.__setattr__(
            self,
            "trusted_context",
            frozenset(str(item).strip() for item in self.trusted_context),
        )
        if self.allowed_approval_modes is not None:
            object.__setattr__(
                self,
                "allowed_approval_modes",
                frozenset(
                    str(item).strip() for item in self.allowed_approval_modes
                ),
            )
        if self.max_risk_level is not None:
            object.__setattr__(
                self,
                "max_risk_level",
                (
                    self.max_risk_level
                    if isinstance(self.max_risk_level, ToolRiskLevel)
                    else ToolRiskLevel(str(self.max_risk_level).lower())
                ),
            )


@dataclass(frozen=True)
class ToolResolution:
    """同一策略下同时给模型和分发层使用的工具选择结果。"""

    definitions: tuple[dict, ...]
    allowed_tool_names: frozenset[str]
    toolsets: frozenset[str]


@dataclass
class ToolEntry:
    """已注册工具的 schema、处理器和运行能力元数据。"""

    name: str
    toolset: str
    schema: dict
    handler: Callable
    execution_environments: frozenset[ExecutionEnvironment]
    unattended_allowed: bool
    required_trusted_context: frozenset[str]
    approval_mode: ApprovalMode
    risk_level: ToolRiskLevel
    default_enabled_environments: frozenset[ExecutionEnvironment]
    retry_safe: bool
    unknown_on_crash: bool
    status_check: Callable | None


class ToolRegistry:
    """全局工具注册表；会话入口只能通过解析结果取得能力。"""

    def __init__(self):
        self._tools: dict[str, ToolEntry] = {}

    def register(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable,
        *,
        execution_environments: Iterable[ExecutionEnvironment | str],
        unattended_allowed: bool,
        required_trusted_context: Iterable[str] = (),
        approval_mode: ApprovalMode | str = ApprovalMode.NONE,
        risk_level: ToolRiskLevel | str = ToolRiskLevel.LOW,
        default_enabled_environments: Iterable[ExecutionEnvironment | str] = (),
        retry_safe: bool = False,
        unknown_on_crash: bool | None = None,
        status_check: Callable | None = None,
    ) -> None:
        """注册一次工具及其跨入口运行策略。"""
        normalized_retry_safe = bool(retry_safe)
        normalized_unknown_on_crash = (
            not normalized_retry_safe
            if unknown_on_crash is None
            else bool(unknown_on_crash)
        )
        if normalized_retry_safe and normalized_unknown_on_crash:
            raise ValueError("retry_safe and unknown_on_crash cannot both be enabled")
        if not normalized_retry_safe and not normalized_unknown_on_crash:
            raise ValueError("a durable recovery policy must be enabled")
        if status_check is not None:
            raise ValueError("status_check tools are not supported until status queries are implemented")
        self._tools[name] = ToolEntry(
            name=name,
            toolset=str(toolset).strip().lower(),
            schema=schema,
            handler=handler,
            execution_environments=frozenset(
                _normalize_environment(item)
                for item in execution_environments
            ),
            unattended_allowed=bool(unattended_allowed),
            required_trusted_context=frozenset(
                str(item).strip() for item in required_trusted_context
            ),
            approval_mode=(
                approval_mode
                if isinstance(approval_mode, ApprovalMode)
                else ApprovalMode(str(approval_mode))
            ),
            risk_level=(
                risk_level
                if isinstance(risk_level, ToolRiskLevel)
                else ToolRiskLevel(str(risk_level))
            ),
            default_enabled_environments=frozenset(
                _normalize_environment(item)
                for item in default_enabled_environments
            ),
            retry_safe=normalized_retry_safe,
            unknown_on_crash=normalized_unknown_on_crash,
            status_check=status_check,
        )

    def get_entry(self, name: str) -> ToolEntry | None:
        """返回工具注册元数据，执行包装器据此决定恢复策略。"""
        return self._tools.get(name)

    def resolve(self, policy: ToolPolicy) -> ToolResolution:
        """按环境、toolset 和可信上下文生成统一的会话能力边界。"""
        definitions: list[dict] = []
        allowed_tool_names: set[str] = set()
        resolved_toolsets: set[str] = set()
        for entry in self._tools.values():
            if policy.environment not in entry.execution_environments:
                continue
            if policy.unattended and not entry.unattended_allowed:
                continue
            if not entry.required_trusted_context.issubset(
                policy.trusted_context
            ):
                continue
            if (
                policy.allowed_approval_modes is not None
                and entry.approval_mode.value not in policy.allowed_approval_modes
            ):
                continue
            if (
                policy.max_risk_level is not None
                and _TOOL_RISK_RANK[entry.risk_level]
                > _TOOL_RISK_RANK[policy.max_risk_level]
            ):
                continue
            if policy.enabled_toolsets is None:
                if policy.environment not in entry.default_enabled_environments:
                    continue
            elif entry.toolset not in policy.enabled_toolsets:
                continue
            definitions.append({"type": "function", "function": entry.schema})
            allowed_tool_names.add(entry.name)
            resolved_toolsets.add(entry.toolset)
        return ToolResolution(
            definitions=tuple(definitions),
            allowed_tool_names=frozenset(allowed_tool_names),
            toolsets=frozenset(resolved_toolsets),
        )

    def toolsets_for_environment(
        self,
        environment: ExecutionEnvironment | str,
    ) -> frozenset[str]:
        """返回由全局元数据声明支持指定入口的工具集。"""
        normalized = _normalize_environment(environment)
        return frozenset(
            entry.toolset
            for entry in self._tools.values()
            if normalized in entry.execution_environments
        )

    def default_toolsets_for_policy(self, policy: ToolPolicy) -> frozenset[str]:
        """返回在给定运行条件下实际默认启用且至少含一个工具的工具集。"""
        if policy.enabled_toolsets is not None:
            raise ValueError("default toolsets require a policy without explicit toolsets")
        return self.resolve(policy).toolsets

    def dispatch(self, name: str, args: dict, **kwargs) -> str:
        """查找工具并调用 handler；调用方必须先执行会话级授权。"""
        entry = self._tools.get(name)
        if not entry:
            return json.dumps({"error": f"Unknown tool: {name}"})
        guard = kwargs.get("cron_capability_guard")
        if guard is not None:
            denial = guard.authorize_tool(name)
            if denial is not None:
                return json.dumps(denial, ensure_ascii=False)
            if name == "skill_view":
                denial = guard.authorize_skill(args.get("name"))
                if denial is not None:
                    return json.dumps(denial, ensure_ascii=False)
        return entry.handler(args, **kwargs)

    def get_definitions(
        self,
        enabled_toolsets: list[str] | None = None,
    ) -> list[dict]:
        """兼容旧调用；新入口应使用 ``resolve`` 取得完整边界。"""
        resolution = self.resolve(
            ToolPolicy(
                ExecutionEnvironment.CLI,
                enabled_toolsets=(
                    None if enabled_toolsets is None else frozenset(enabled_toolsets)
                ),
            )
        )
        return list(resolution.definitions)


registry = ToolRegistry()


def register_all() -> None:
    """导入并注册所有工具；重复调用不会改变最终注册表。"""
    from hermes.tools.terminal import register as _terminal
    from hermes.tools.file import register as _file
    from hermes.tools.memory import register as _memory
    from hermes.tools.skill import register as _skill
    from hermes.tools.delegate import register as _delegate
    from hermes.tools.gateway_send_file import register as _gateway_send_file
    from hermes.cron.tool import register as _cron

    _terminal(registry)
    _file(registry)
    _memory(registry)
    _skill(registry)
    _delegate(registry)
    _gateway_send_file(registry)
    _cron(registry)
