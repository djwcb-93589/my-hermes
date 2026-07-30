"""工具能力声明的轻量契约，不包含任何运行时对象。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from hermes.observability.contracts import (
    CapabilityDescriptor,
    ToolsetDescriptor,
)
from hermes.tool_policy import (
    ApprovalMode,
    ExecutionEnvironment,
    ToolRiskLevel,
    normalize_approval_mode,
    normalize_execution_environment,
    normalize_tool_risk_level,
)


def _required_text(value: object, field_name: str) -> str:
    """校验声明中的稳定文本字段。"""
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    """规范化声明中的字符串集合。"""
    if not isinstance(value, (tuple, list, set, frozenset)):
        raise TypeError(f"{field_name} must be a collection of strings")
    return tuple(sorted({_required_text(item, field_name) for item in value}))


def _environment_tuple(
    value: object,
    field_name: str,
    *,
    required: bool,
) -> tuple[str, ...]:
    """按共享策略定义校验并规范化执行环境。"""
    if not isinstance(value, (tuple, list, set, frozenset)):
        raise TypeError(f"{field_name} must be a collection of environments")
    normalized: set[str] = set()
    for item in value:
        if isinstance(item, ExecutionEnvironment):
            environment = item
        else:
            environment = normalize_execution_environment(
                _required_text(item, field_name)
            )
        normalized.add(environment.value)
    if required and not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return tuple(sorted(normalized))


def _approval_mode(value: object) -> str:
    """按共享策略定义校验审批模式。"""
    if isinstance(value, ApprovalMode):
        return value.value
    return normalize_approval_mode(
        _required_text(value, "approval_mode").lower()
    ).value


def _risk_level(value: object) -> str:
    """按共享策略定义校验风险等级。"""
    if isinstance(value, ToolRiskLevel):
        return value.value
    return normalize_tool_risk_level(
        _required_text(value, "risk_level").lower()
    ).value


def _required_bool(value: object, field_name: str) -> bool:
    """拒绝把其他真值对象隐式转换为布尔策略。"""
    if type(value) is not bool:
        raise TypeError(f"{field_name} must be a boolean")
    return value


def _freeze_schema(value: object, path: str) -> object:
    """冻结 JSON Schema，避免声明快照持有可变结构。"""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} must not contain a non-finite number")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, child in value.items():
            if type(key) is not str or not key:
                raise TypeError(f"{path} mapping keys must be non-empty strings")
            frozen[key] = _freeze_schema(child, f"{path}.{key}")
        return MappingProxyType(frozen)
    if type(value) in (list, tuple):
        return tuple(
            _freeze_schema(child, f"{path}[{index}]")
            for index, child in enumerate(value)
        )
    raise TypeError(f"{path} contains an unsupported schema value")


def _mutable_schema_copy(value: object) -> object:
    """为运行时注册生成新的普通 JSON Schema 副本。"""
    if isinstance(value, Mapping):
        return {key: _mutable_schema_copy(child) for key, child in value.items()}
    if type(value) is tuple:
        return [_mutable_schema_copy(child) for child in value]
    return value


def _validate_schema_shape(schema: Mapping[str, object], name: str) -> None:
    """校验能力目录和运行时共享的最小 schema 结构。"""
    schema_name = schema.get("name")
    if schema_name is not None and schema_name != name:
        raise ValueError("tool schema name must match declaration name")
    description = schema.get("description", "")
    if type(description) is not str:
        raise ValueError("tool schema description must be a string")
    if "parameters" not in schema:
        return
    parameters = schema["parameters"]
    if not isinstance(parameters, Mapping):
        raise ValueError("tool schema parameters must be a mapping")
    properties = parameters.get("properties", {})
    if not isinstance(properties, Mapping) or any(
        type(field_name) is not str or not field_name
        for field_name in properties
    ):
        raise ValueError("tool schema properties must have string names")
    required = parameters.get("required", ())
    if not isinstance(required, (tuple, list)) or any(
        type(field_name) is not str or not field_name
        for field_name in required
    ):
        raise ValueError("tool schema required must be a list of strings")
    if not set(required).issubset(properties):
        raise ValueError("tool schema required names must exist in properties")


@dataclass(frozen=True, slots=True)
class ToolDeclaration:
    """不持有 handler 的工具声明；运行时与目录均从此对象读取策略。"""

    name: str
    toolset: str
    schema: Mapping[str, object]
    execution_environments: tuple[str, ...]
    default_enabled_environments: tuple[str, ...]
    unattended_allowed: bool
    approval_mode: str
    risk_level: str
    retry_safe: bool = False
    unknown_on_crash: bool = True
    supports_cancellation: bool = False
    has_status_check: bool = False
    required_trusted_context: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """冻结 schema 并验证恢复和授权策略。"""
        object.__setattr__(self, "name", _required_text(self.name, "name"))
        object.__setattr__(
            self,
            "toolset",
            _required_text(self.toolset, "toolset").lower(),
        )
        frozen_schema = _freeze_schema(self.schema, "schema")
        assert isinstance(frozen_schema, Mapping)
        _validate_schema_shape(frozen_schema, self.name)
        object.__setattr__(self, "schema", frozen_schema)
        object.__setattr__(
            self,
            "execution_environments",
            _environment_tuple(
                self.execution_environments,
                "execution_environments",
                required=True,
            ),
        )
        object.__setattr__(
            self,
            "default_enabled_environments",
            _environment_tuple(
                self.default_enabled_environments,
                "default_enabled_environments",
                required=False,
            ),
        )
        if not set(self.default_enabled_environments).issubset(
            self.execution_environments
        ):
            raise ValueError(
                "default_enabled_environments must be a subset of "
                "execution_environments"
            )
        object.__setattr__(
            self,
            "required_trusted_context",
            _string_tuple(
                self.required_trusted_context,
                "required_trusted_context",
            ),
        )
        object.__setattr__(
            self,
            "unattended_allowed",
            _required_bool(self.unattended_allowed, "unattended_allowed"),
        )
        object.__setattr__(
            self,
            "retry_safe",
            _required_bool(self.retry_safe, "retry_safe"),
        )
        object.__setattr__(
            self,
            "unknown_on_crash",
            _required_bool(self.unknown_on_crash, "unknown_on_crash"),
        )
        object.__setattr__(
            self,
            "supports_cancellation",
            _required_bool(
                self.supports_cancellation,
                "supports_cancellation",
            ),
        )
        object.__setattr__(
            self,
            "has_status_check",
            _required_bool(self.has_status_check, "has_status_check"),
        )
        object.__setattr__(
            self,
            "approval_mode",
            _approval_mode(self.approval_mode),
        )
        object.__setattr__(
            self,
            "risk_level",
            _risk_level(self.risk_level),
        )
        if self.retry_safe and self.unknown_on_crash:
            raise ValueError(
                "retry_safe and unknown_on_crash cannot both be enabled"
            )
        if not self.retry_safe and not self.unknown_on_crash:
            raise ValueError("a durable recovery policy must be enabled")

    def runtime_schema(self) -> dict[str, object]:
        """返回供 ToolRegistry 注册使用的独立 schema 副本。"""
        copied = _mutable_schema_copy(self.schema)
        assert isinstance(copied, dict)
        return copied


def capability_from_declaration(
    declaration: ToolDeclaration,
) -> CapabilityDescriptor:
    """把轻量声明投影为不含 schema 引用的能力描述。"""
    if not isinstance(declaration, ToolDeclaration):
        raise TypeError("declaration must be a ToolDeclaration")
    parameters = declaration.schema.get("parameters", {})
    assert isinstance(parameters, Mapping)
    properties = parameters.get("properties", {})
    required = parameters.get("required", ())
    assert isinstance(properties, Mapping)
    assert isinstance(required, (tuple, list))
    description = declaration.schema.get("description", "")
    assert type(description) is str
    return CapabilityDescriptor(
        name=declaration.name,
        toolset=declaration.toolset,
        description=description,
        parameter_names=tuple(sorted(properties)),
        required_parameters=tuple(sorted(required)),
        execution_environments=declaration.execution_environments,
        default_enabled_environments=(
            declaration.default_enabled_environments
        ),
        unattended_allowed=declaration.unattended_allowed,
        approval_mode=declaration.approval_mode,
        risk_level=declaration.risk_level,
        retry_safe=declaration.retry_safe,
        unknown_on_crash=declaration.unknown_on_crash,
        supports_cancellation=declaration.supports_cancellation,
        has_status_check=declaration.has_status_check,
    )


def toolsets_from_capabilities(
    capabilities: tuple[CapabilityDescriptor, ...],
) -> tuple[ToolsetDescriptor, ...]:
    """按能力描述聚合 Toolset，供运行时和 Dashboard 共同复用。"""
    grouped: dict[str, list[CapabilityDescriptor]] = {}
    for capability in capabilities:
        if not isinstance(capability, CapabilityDescriptor):
            raise TypeError("capabilities must contain CapabilityDescriptor")
        grouped.setdefault(capability.toolset, []).append(capability)
    return tuple(
        ToolsetDescriptor(
            name=toolset,
            tool_names=tuple(sorted(item.name for item in items)),
            execution_environments=tuple(sorted({
                environment
                for item in items
                for environment in item.execution_environments
            })),
            default_enabled_environments=tuple(sorted({
                environment
                for item in items
                for environment in item.default_enabled_environments
            })),
        )
        for toolset, items in sorted(grouped.items())
    )


__all__ = [
    "ToolDeclaration",
    "capability_from_declaration",
    "toolsets_from_capabilities",
]
