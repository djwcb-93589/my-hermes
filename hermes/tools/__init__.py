"""全局工具注册表与按运行策略解析工具能力。"""

from __future__ import annotations

import json
import importlib
import logging
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable

from hermes.observability.contracts import (
    CapabilityDescriptor,
    ToolsetDescriptor,
)


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


_METADATA_IMPORT_DEPTH = 0


@contextmanager
def _metadata_registration_import_scope():
    """标记仅声明元数据的工具模块导入，避免其导入运行时配置。"""
    global _METADATA_IMPORT_DEPTH
    _METADATA_IMPORT_DEPTH += 1
    try:
        yield
    finally:
        _METADATA_IMPORT_DEPTH -= 1


def _metadata_registration_import_active() -> bool:
    """供工具模块在导入期判断是否只能加载声明内容。"""
    return _METADATA_IMPORT_DEPTH > 0


def _metadata_only_handler(*args, **kwargs) -> str:
    """防止 Metadata-only Registry 的条目被直接当作可执行工具调用。"""
    del args, kwargs
    raise RuntimeError("metadata-only tool registry cannot execute tools")


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
    has_status_check: bool = False


class ToolRegistry:
    """全局工具注册表；会话入口只能通过解析结果取得能力。"""

    def __init__(self, *, metadata_only: bool = False):
        if not isinstance(metadata_only, bool):
            raise TypeError("metadata_only must be a boolean")
        self._tools: dict[str, ToolEntry] = {}
        self._metadata_only = metadata_only

    @property
    def metadata_only(self) -> bool:
        """标识此 Registry 只能提供声明快照，不能执行工具。"""
        return self._metadata_only

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
            handler=(
                _metadata_only_handler if self._metadata_only else handler
            ),
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
            status_check=None if self._metadata_only else status_check,
            supports_cancellation=supports_cancellation,
            has_status_check=status_check is not None,
        )

    def merge_from(self, other_registry: "ToolRegistry") -> None:
        """校验来源注册表后原子合并其全部工具。"""
        if not isinstance(other_registry, ToolRegistry):
            raise TypeError("other_registry must be a ToolRegistry")
        if other_registry is self:
            return

        validated_registry = ToolRegistry(metadata_only=self.metadata_only)
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
            if self._metadata_only:
                validated_registry._tools[entry.name].has_status_check = (
                    entry.has_status_check
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
            _capability_descriptor(entry)
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
        grouped: dict[str, list[CapabilityDescriptor]] = {}
        for descriptor in self.describe_capabilities():
            grouped.setdefault(descriptor.toolset, []).append(descriptor)
        return tuple(
            ToolsetDescriptor(
                name=toolset,
                tool_names=tuple(
                    sorted(item.name for item in descriptors)
                ),
                execution_environments=tuple(sorted({
                    environment
                    for item in descriptors
                    for environment in item.execution_environments
                })),
                default_enabled_environments=tuple(sorted({
                    environment
                    for item in descriptors
                    for environment in item.default_enabled_environments
                })),
            )
            for toolset, descriptors in sorted(grouped.items())
        )

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
        if self._metadata_only:
            raise RuntimeError("metadata-only tool registry cannot dispatch tools")
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
        if self._metadata_only:
            raise RuntimeError("metadata-only tool registry cannot dispatch tools")
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
        if self._metadata_only:
            raise RuntimeError("metadata-only tool registry cannot dispatch tools")
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


def _capability_descriptor(entry: ToolEntry) -> CapabilityDescriptor:
    """从单个 ToolEntry 提取不含 Callable 或 schema 引用的能力描述。"""
    _validate_tool_schema(entry.schema)
    parameters = entry.schema.get("parameters", {})
    assert isinstance(parameters, dict)
    properties = parameters.get("properties", {})
    required = parameters.get("required", ())
    assert isinstance(properties, dict)
    assert isinstance(required, (list, tuple))
    return CapabilityDescriptor(
        name=entry.name,
        toolset=entry.toolset,
        description=entry.schema.get("description", ""),
        parameter_names=tuple(sorted(properties)),
        required_parameters=tuple(sorted(required)),
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
        has_status_check=(
            entry.has_status_check or entry.status_check is not None
        ),
    )


def _registration_module(
    module_name: str,
    target: ToolRegistry,
):
    """按目标模式加载工具声明模块，必要时把声明导入重载为运行时导入。"""
    if target.metadata_only:
        with _metadata_registration_import_scope():
            return importlib.import_module(module_name)
    module = importlib.import_module(module_name)
    package_name = module_name.rpartition(".")[0]
    package = sys.modules.get(package_name)
    if package is not None and getattr(package, "__hermes_metadata_only__", False):
        importlib.reload(package)
    if getattr(module, "__hermes_metadata_only__", False):
        return importlib.reload(module)
    return module


def register_all(target_registry: ToolRegistry | None = None) -> None:
    """导入并注册所有工具；重复调用不会改变最终注册表。"""
    target = registry if target_registry is None else target_registry
    _terminal = _registration_module("hermes.tools.terminal", target)
    _file = _registration_module("hermes.tools.file", target)
    _memory = _registration_module("hermes.tools.memory", target)
    _delegate = _registration_module("hermes.tools.delegate", target)
    _gateway_send_file = _registration_module(
        "hermes.tools.gateway_send_file",
        target,
    )
    _media = _registration_module("hermes.tools.media", target)
    _browser = _registration_module("hermes.tools.browser", target)
    _cron = _registration_module("hermes.cron.tool", target)

    _terminal.register(target)
    _file.register(target)
    _memory.register(target)
    try:
        skill_registry = ToolRegistry(metadata_only=target.metadata_only)
        _skill = _registration_module("hermes.tools.skill", target)
        _skill.register(skill_registry)
        if all(
            target.get_entry(name) == entry
            for name, entry in skill_registry._tools.items()
        ):
            # 重复初始化时保留完全一致的 Skill 工具，避免误报名称冲突。
            pass
        else:
            target.merge_from(skill_registry)
    except Exception as exc:
        if target.metadata_only:
            raise
        logger.warning(
            "Skill tools unavailable; Skill capability was skipped: %s",
            type(exc).__name__,
            exc_info=True,
        )
    _delegate.register(target)
    _gateway_send_file.register(target)
    _media.register(target)
    _browser.register(target)
    _cron.register(target)
