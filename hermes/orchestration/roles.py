"""单任务 Worker 使用的轻量、显式 Agent Role 配置。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from hermes.orchestration.errors import UnknownAgentRoleError


_MAX_ROLE_NAME_LENGTH = 128
_MAX_ROLE_PROMPT_LENGTH = 100_000
_MAX_MODEL_NAME_LENGTH = 512
_MAX_TOOLSET_NAME_LENGTH = 128
_MAX_ROLE_ITERATIONS = 1_000
_FORBIDDEN_MODEL_KWARG_KEYS = frozenset({
    "api_client",
    "api_key",
    "authorization",
    "claim_token",
    "password",
    "process_manager",
    "session",
    "session_key",
    "tool_registry",
    "workflow",
    "workflow_id",
})


def _freeze_value(value: object) -> object:
    """递归冻结普通数据，并拒绝把运行时对象藏入角色计划。"""

    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("model_kwargs must contain finite numbers")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str or not key:
                raise ValueError(
                    "model_kwargs must contain non-empty string keys"
                )
            if key.strip().lower() in _FORBIDDEN_MODEL_KWARG_KEYS:
                raise ValueError("model_kwargs contains forbidden runtime data")
            frozen[key] = _freeze_value(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_value(item) for item in value)
    raise TypeError("model_kwargs must contain only plain data")


def _freeze_model_kwargs(
    value: Mapping[str, object],
) -> Mapping[str, object]:
    """冻结模型关键字参数，并拒绝不稳定的非字符串参数名。"""

    if not isinstance(value, Mapping):
        raise TypeError("model_kwargs must be a mapping")
    if any(type(key) is not str or not key for key in value):
        raise ValueError("model_kwargs keys must be non-empty strings")
    frozen = _freeze_value(value)
    if not isinstance(frozen, Mapping):
        raise TypeError("model_kwargs must be a mapping")
    return frozen


def _require_text(value: object, field_name: str, maximum: int) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if len(value) > maximum:
        raise ValueError(f"{field_name} exceeds its length limit")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must contain valid Unicode") from exc
    return value


@dataclass(frozen=True, slots=True)
class AgentRoleSpec:
    """调用方显式注入的不可变单 Agent 执行角色。"""

    name: str
    system_prompt: str
    toolsets: tuple[str, ...]
    model: str
    max_iterations: int
    model_kwargs: Mapping[str, object]

    def __post_init__(self) -> None:
        _require_text(self.name, "role name", _MAX_ROLE_NAME_LENGTH)
        _require_text(
            self.system_prompt,
            "role system_prompt",
            _MAX_ROLE_PROMPT_LENGTH,
        )
        _require_text(self.model, "role model", _MAX_MODEL_NAME_LENGTH)
        if not isinstance(self.toolsets, (list, tuple)) or not self.toolsets:
            raise ValueError("role toolsets must be a non-empty sequence")
        normalized_toolsets = tuple(
            _require_text(
                toolset,
                "role toolset",
                _MAX_TOOLSET_NAME_LENGTH,
            ).strip().lower()
            for toolset in self.toolsets
        )
        if len(normalized_toolsets) != len(set(normalized_toolsets)):
            raise ValueError("role toolsets must not contain duplicates")
        if (
            type(self.max_iterations) is not int
            or not 1 <= self.max_iterations <= _MAX_ROLE_ITERATIONS
        ):
            raise ValueError(
                "role max_iterations must be a positive integer within its limit"
            )
        object.__setattr__(self, "toolsets", normalized_toolsets)
        object.__setattr__(
            self,
            "model_kwargs",
            _freeze_model_kwargs(self.model_kwargs),
        )


class RoleResolver(Protocol):
    """把持久化角色名称解析为完整不可变执行计划。"""

    def resolve(self, role_name: str) -> AgentRoleSpec:
        """解析角色；未知名称必须抛出 UnknownAgentRoleError。"""


class StaticRoleRegistry:
    """构造后不可修改、无全局实例的最小内存 RoleResolver。"""

    __slots__ = ("_roles",)

    def __init__(self, roles: Mapping[str, AgentRoleSpec]) -> None:
        if not isinstance(roles, Mapping) or not roles:
            raise TypeError("roles must be a non-empty mapping")
        validated: dict[str, AgentRoleSpec] = {}
        for name, spec in roles.items():
            _require_text(name, "role registry name", _MAX_ROLE_NAME_LENGTH)
            if not isinstance(spec, AgentRoleSpec):
                raise TypeError("roles must contain AgentRoleSpec values")
            if name != spec.name:
                raise ValueError("role registry key must match AgentRoleSpec.name")
            if name in validated:
                raise ValueError("role registry names must be unique")
            validated[name] = spec
        self._roles = MappingProxyType(validated)

    def resolve(self, role_name: str) -> AgentRoleSpec:
        if type(role_name) is not str or not role_name.strip():
            raise UnknownAgentRoleError("agent role is not registered")
        role = self._roles.get(role_name)
        if role is None:
            raise UnknownAgentRoleError("agent role is not registered")
        return role


__all__ = [
    "AgentRoleSpec",
    "RoleResolver",
    "StaticRoleRegistry",
]
