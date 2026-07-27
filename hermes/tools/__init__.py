"""全局工具注册表与按运行策略解析工具能力。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable


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


logger = logging.getLogger(__name__)


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
    supports_cancellation: bool = False


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
        supports_cancellation: bool = False,
    ) -> None:
        """注册一次工具及其跨入口运行策略。"""
        if not isinstance(supports_cancellation, bool):
            raise ValueError("supports_cancellation must be a boolean")
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
        if status_check is not None and not callable(status_check):
            raise ValueError("status_check must be callable")
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
            supports_cancellation=supports_cancellation,
        )

    def merge_from(self, other_registry: "ToolRegistry") -> None:
        """校验来源注册表后原子合并其全部工具。"""
        if not isinstance(other_registry, ToolRegistry):
            raise TypeError("other_registry must be a ToolRegistry")
        if other_registry is self:
            return

        validated_registry = ToolRegistry()
        for name, entry in other_registry._tools.items():
            if (
                not isinstance(name, str)
                or not name.strip()
                or not isinstance(entry, ToolEntry)
                or entry.name != name
                or not isinstance(entry.toolset, str)
                or not entry.toolset.strip()
                or not isinstance(entry.schema, dict)
                or not callable(entry.handler)
                or not isinstance(entry.execution_environments, frozenset)
                or not all(
                    isinstance(environment, ExecutionEnvironment)
                    for environment in entry.execution_environments
                )
                or not isinstance(entry.unattended_allowed, bool)
                or not isinstance(entry.required_trusted_context, frozenset)
                or not all(
                    isinstance(context, str)
                    for context in entry.required_trusted_context
                )
                or not isinstance(entry.approval_mode, ApprovalMode)
                or not isinstance(entry.risk_level, ToolRiskLevel)
                or not isinstance(entry.default_enabled_environments, frozenset)
                or not all(
                    isinstance(environment, ExecutionEnvironment)
                    for environment in entry.default_enabled_environments
                )
                or not isinstance(entry.retry_safe, bool)
                or not isinstance(entry.unknown_on_crash, bool)
                or (
                    entry.status_check is not None
                    and not callable(entry.status_check)
                )
                or not isinstance(entry.supports_cancellation, bool)
            ):
                raise ValueError("source registry contains an invalid tool entry")
            validated_registry.register(
                entry.name,
                entry.toolset,
                entry.schema,
                entry.handler,
                execution_environments=entry.execution_environments,
                unattended_allowed=entry.unattended_allowed,
                required_trusted_context=entry.required_trusted_context,
                approval_mode=entry.approval_mode,
                risk_level=entry.risk_level,
                default_enabled_environments=entry.default_enabled_environments,
                retry_safe=entry.retry_safe,
                unknown_on_crash=entry.unknown_on_crash,
                status_check=entry.status_check,
                supports_cancellation=entry.supports_cancellation,
            )

        conflicts = self._tools.keys() & validated_registry._tools.keys()
        if conflicts:
            names = ", ".join(sorted(conflicts))
            raise ValueError(f"tool registry merge has name conflicts: {names}")

        merged_tools = dict(self._tools)
        merged_tools.update(validated_registry._tools)
        self._tools = merged_tools

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
        handler_kwargs = dict(kwargs)
        allowed_tool_names = kwargs.get("allowed_tool_names")
        if (
            allowed_tool_names is not None
            and name not in frozenset(str(item) for item in allowed_tool_names)
        ):
            return json.dumps({
                "ok": False,
                "error_type": "tool_not_authorized",
                "error": "tool is outside the current execution boundary",
            }, ensure_ascii=False)
        guard = kwargs.get("cron_capability_guard")
        if guard is not None:
            denial = guard.authorize_tool(name)
            if denial is not None:
                return json.dumps(denial, ensure_ascii=False)
            if name == "skill_view":
                denial = guard.authorize_skill(args.get("name"))
                if denial is not None:
                    return json.dumps(denial, ensure_ascii=False)
        if (
            "cancel_checker" in handler_kwargs
            and not entry.supports_cancellation
        ):
            handler_kwargs.pop("cancel_checker")
        return entry.handler(args, **handler_kwargs)

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


def register_all(target_registry: ToolRegistry | None = None) -> None:
    """导入并注册所有工具；重复调用不会改变最终注册表。"""
    target = registry if target_registry is None else target_registry
    from hermes.tools.terminal import register as _terminal
    from hermes.tools.file import register as _file
    from hermes.tools.memory import register as _memory
    from hermes.tools.delegate import register as _delegate
    from hermes.tools.gateway_send_file import register as _gateway_send_file
    from hermes.tools.media import register as _media
    from hermes.tools.browser import register as _browser
    from hermes.cron.tool import register as _cron

    _terminal(target)
    _file(target)
    _memory(target)
    try:
        skill_registry = ToolRegistry()
        from hermes.tools.skill import register as _skill

        _skill(skill_registry)
        if all(
            target.get_entry(name) == entry
            for name, entry in skill_registry._tools.items()
        ):
            # 重复初始化时保留完全一致的 Skill 工具，避免误报名称冲突。
            pass
        else:
            target.merge_from(skill_registry)
    except Exception as exc:
        logger.warning(
            "Skill tools unavailable; Skill capability was skipped: %s",
            type(exc).__name__,
            exc_info=True,
        )
    _delegate(target)
    _gateway_send_file(target)
    _media(target)
    _browser(target)
    _cron(target)
