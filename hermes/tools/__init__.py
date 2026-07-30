"""全局工具注册表与按运行策略解析工具能力。"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Callable, Iterable

from hermes.observability.contracts import (
    CapabilityDescriptor,
    ToolsetDescriptor,
)
from hermes.tool_declarations.contracts import (
    ToolDeclaration,
    capability_from_declaration,
    toolsets_from_capabilities,
)
from hermes.tool_policy import (
    ApprovalMode,
    ExecutionEnvironment,
    ToolRiskLevel,
    normalize_approval_mode,
    normalize_execution_environment,
    normalize_tool_risk_level,
    tool_risk_rank,
)


logger = logging.getLogger(__name__)


def _normalize_environment(
    value: ExecutionEnvironment | str,
) -> ExecutionEnvironment:
    """兼容枚举值和外部传入的小写环境名称。"""
    return normalize_execution_environment(value)


@dataclass(frozen=True)
class ToolPolicy:
    """一次会话解析工具时使用的运行策略。"""

    environment: ExecutionEnvironment | str
    enabled_toolsets: frozenset[str] | None = None
    unattended: bool = False
    trusted_context: frozenset[str] = field(default_factory=frozenset)
    allowed_approval_modes: frozenset[ApprovalMode | str] | None = None
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
                    normalize_approval_mode(item)
                    for item in self.allowed_approval_modes
                ),
            )
        if self.max_risk_level is not None:
            object.__setattr__(
                self,
                "max_risk_level",
                normalize_tool_risk_level(
                    self.max_risk_level
                    if isinstance(self.max_risk_level, ToolRiskLevel)
                    else str(self.max_risk_level).lower()
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
    has_status_check: bool = False


class ToolRegistry:
    """全局工具注册表；会话入口只能通过解析结果取得能力。"""

    def __init__(self):
        self._tools: dict[str, ToolEntry] = {}
        self._default_tools_registered = False

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
        if type(name) is not str or not name.strip():
            raise ValueError("tool name must be a non-empty string")
        if name in self._tools:
            raise ValueError(f"tool is already registered: {name}")
        _validate_tool_schema(schema)
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
                normalize_approval_mode(approval_mode)
            ),
            risk_level=(
                normalize_tool_risk_level(risk_level)
            ),
            default_enabled_environments=frozenset(
                _normalize_environment(item)
                for item in default_enabled_environments
            ),
            retry_safe=normalized_retry_safe,
            unknown_on_crash=normalized_unknown_on_crash,
            status_check=status_check,
            supports_cancellation=supports_cancellation,
            has_status_check=status_check is not None,
        )

    def register_declaration(
        self,
        declaration: ToolDeclaration,
        handler: Callable,
        *,
        status_check: Callable | None = None,
    ) -> None:
        """以共享轻量声明注册真实 handler，不创建第二套 schema。"""
        if not isinstance(declaration, ToolDeclaration):
            raise TypeError("declaration must be a ToolDeclaration")
        if not callable(handler):
            raise ValueError("handler must be callable")
        if declaration.has_status_check != (status_check is not None):
            raise ValueError("status_check must match the tool declaration")
        self.register(
            name=declaration.name,
            toolset=declaration.toolset,
            schema=declaration.runtime_schema(),
            handler=handler,
            execution_environments=declaration.execution_environments,
            unattended_allowed=declaration.unattended_allowed,
            required_trusted_context=declaration.required_trusted_context,
            approval_mode=declaration.approval_mode,
            risk_level=declaration.risk_level,
            default_enabled_environments=(
                declaration.default_enabled_environments
            ),
            retry_safe=declaration.retry_safe,
            unknown_on_crash=declaration.unknown_on_crash,
            status_check=status_check,
            supports_cancellation=declaration.supports_cancellation,
        )

    def merge_from(self, other_registry: "ToolRegistry") -> None:
        """校验来源注册表后原子合并其全部工具。"""
        if not isinstance(other_registry, ToolRegistry):
            raise TypeError("other_registry must be a ToolRegistry")
        if other_registry is self:
            raise ValueError("cannot merge a tool registry into itself")

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
                or not isinstance(entry.has_status_check, bool)
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

    def describe_capabilities(self) -> tuple[CapabilityDescriptor, ...]:
        """返回当前声明能力的稳定不可变快照，不进行会话级授权过滤。"""
        descriptors = tuple(
            capability_from_declaration(_declaration_from_entry(entry))
            for entry in self._tools.values()
        )
        return tuple(
            sorted(descriptors, key=lambda descriptor: (
                descriptor.toolset,
                descriptor.name,
            ))
        )

    def describe_toolsets(self) -> tuple[ToolsetDescriptor, ...]:
        """按能力快照聚合工具集，不调用 Handler 或 status_check。"""
        return toolsets_from_capabilities(self.describe_capabilities())

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
                and entry.approval_mode not in policy.allowed_approval_modes
            ):
                continue
            if (
                policy.max_risk_level is not None
                and tool_risk_rank(entry.risk_level)
                > tool_risk_rank(policy.max_risk_level)
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
        return self._dispatch_entry(entry, args, **kwargs)

    def _dispatch_verified_entry(
        self,
        entry: ToolEntry,
        args: dict,
        **kwargs,
    ) -> str:
        """仅供已完成 AgentLoop 策略校验的内部调用复用注册条目。"""
        if not isinstance(entry, ToolEntry):
            raise TypeError("entry must be a ToolEntry")
        return self._dispatch_entry_core(entry, args, **kwargs)

    def _dispatch_entry(
        self,
        entry: ToolEntry,
        args: dict,
        **kwargs,
    ) -> str:
        """统一执行注册条目及其不可绕过的运行时安全检查。"""
        if not isinstance(entry, ToolEntry):
            raise TypeError("entry must be a ToolEntry")
        name = entry.name
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
        return self._dispatch_entry_core(entry, args, **kwargs)

    @staticmethod
    def _dispatch_entry_core(
        entry: ToolEntry,
        args: dict,
        **kwargs,
    ) -> str:
        """执行保留给所有入口的 cron、取消和 handler 契约逻辑。"""
        name = entry.name
        handler_kwargs = dict(kwargs)
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


def register_declared_handlers(
    target_registry: ToolRegistry,
    declarations: Iterable[ToolDeclaration],
    handlers_by_name: Mapping[str, Callable],
) -> None:
    """完整校验名称绑定后注册一组共享声明。"""
    if not isinstance(target_registry, ToolRegistry):
        raise TypeError("target_registry must be a ToolRegistry")

    declaration_items = tuple(declarations)
    if not declaration_items or not all(
        isinstance(item, ToolDeclaration)
        for item in declaration_items
    ):
        raise TypeError("declarations must contain ToolDeclaration")
    declaration_names = tuple(
        declaration.name
        for declaration in declaration_items
    )
    duplicate_names = tuple(sorted(
        name
        for name, count in Counter(declaration_names).items()
        if count > 1
    ))
    if duplicate_names:
        raise ValueError(
            "tool declarations contain duplicate names: "
            + ", ".join(duplicate_names)
        )
    if not isinstance(handlers_by_name, Mapping):
        raise TypeError("handlers_by_name must be a mapping")
    if any(
        type(name) is not str or not name
        for name in handlers_by_name
    ):
        raise TypeError("handler names must be non-empty strings")

    expected_names = set(declaration_names)
    handler_names = set(handlers_by_name)
    missing_names = tuple(sorted(expected_names - handler_names))
    if missing_names:
        raise ValueError(
            "tool declarations have missing handlers: "
            + ", ".join(missing_names)
        )
    extra_names = tuple(sorted(handler_names - expected_names))
    if extra_names:
        raise ValueError(
            "tool declarations have extra handlers: "
            + ", ".join(extra_names)
        )
    uncallable_names = tuple(sorted({
        name
        for name in handler_names
        if not callable(handlers_by_name[name])
    }))
    if uncallable_names:
        raise TypeError(
            "tool declaration handlers must be callable: "
            + ", ".join(uncallable_names)
        )

    staging_registry = ToolRegistry()
    for declaration in declaration_items:
        staging_registry.register_declaration(
            declaration,
            handlers_by_name[declaration.name],
        )
    target_registry.merge_from(staging_registry)


registry = ToolRegistry()


def _validate_tool_schema(schema: object) -> None:
    """在注册阶段确认能力目录所需 schema 结构完整且只含名称字段。"""
    if type(schema) is not dict:
        raise ValueError("tool schema must be a dict")
    if "description" not in schema:
        description = ""
    else:
        description = schema["description"]
    if type(description) is not str:
        raise ValueError("tool schema description must be a string")
    if "parameters" not in schema:
        return
    parameters = schema["parameters"]
    if type(parameters) is not dict:
        raise ValueError("tool schema parameters must be a dict")
    properties = parameters.get("properties", {})
    if type(properties) is not dict or any(
        type(name) is not str or not name
        for name in properties
    ):
        raise ValueError("tool schema properties must have string names")
    required = parameters.get("required", ())
    if type(required) not in (list, tuple) or any(
        type(name) is not str or not name
        for name in required
    ):
        raise ValueError("tool schema required must be a list of strings")
    if not set(required).issubset(properties):
        raise ValueError("tool schema required names must exist in properties")


def _declaration_from_entry(entry: ToolEntry) -> ToolDeclaration:
    """将运行时条目投影为共享的轻量声明，再复用统一描述转换。"""
    _validate_tool_schema(entry.schema)
    return ToolDeclaration(
        name=entry.name,
        toolset=entry.toolset,
        schema=entry.schema,
        execution_environments=tuple(sorted(
            environment.value for environment in entry.execution_environments
        )),
        default_enabled_environments=tuple(sorted(
            environment.value
            for environment in entry.default_enabled_environments
        )),
        unattended_allowed=entry.unattended_allowed,
        approval_mode=entry.approval_mode.value,
        risk_level=entry.risk_level.value,
        retry_safe=entry.retry_safe,
        unknown_on_crash=entry.unknown_on_crash,
        supports_cancellation=entry.supports_cancellation,
        has_status_check=entry.has_status_check or entry.status_check is not None,
        required_trusted_context=tuple(sorted(entry.required_trusted_context)),
    )


def register_all(target_registry: ToolRegistry | None = None) -> None:
    """导入并注册所有工具；重复调用不会改变最终注册表。"""
    target = registry if target_registry is None else target_registry
    if not isinstance(target, ToolRegistry):
        raise TypeError("target_registry must be a ToolRegistry")
    if target._default_tools_registered:
        return
    staging_registry = ToolRegistry()
    from hermes.tools.terminal import register as _terminal
    from hermes.tools.file import register as _file
    from hermes.tools.memory import register as _memory
    from hermes.tools.delegate import register as _delegate
    from hermes.tools.gateway_send_file import register as _gateway_send_file
    from hermes.tools.media import register as _media
    from hermes.tools.browser import register as _browser
    from hermes.cron.tool import register as _cron

    _terminal(staging_registry)
    _file(staging_registry)
    _memory(staging_registry)
    try:
        from hermes.tools.skill import register as _skill

        skill_registry = ToolRegistry()
        _skill(skill_registry)
        staging_registry.merge_from(skill_registry)
    except Exception as exc:
        logger.warning(
            "Skill tools unavailable; Skill capability was skipped: %s",
            type(exc).__name__,
        )
    _delegate(staging_registry)
    _gateway_send_file(staging_registry)
    _media(staging_registry)
    _browser(staging_registry)
    _cron(staging_registry)
    target.merge_from(staging_registry)
    target._default_tools_registered = True
