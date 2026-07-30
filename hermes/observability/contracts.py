"""可由工具、Hook 与未来运行时模块共同使用的中立可观测契约。"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol


_MAX_METADATA_DEPTH = 8
_MAX_METADATA_ITEMS = 256
_MAX_METADATA_TEXT_CHARS = 16_384
_SENSITIVE_METADATA_KEYS = frozenset({
    "access_key",
    "api_key",
    "apikey",
    "client_secret",
    "private_key",
    "refresh_token",
})
_SENSITIVE_METADATA_KEY_PARTS = frozenset({
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
})
_DISALLOWED_METADATA_KEY_PARTS = frozenset({
    "arguments",
    "content",
    "exception",
    "message",
    "messages",
    "model_response",
    "prompt",
    "raw_response",
    "response",
    "result",
    "stacktrace",
    "traceback",
})
_ERROR_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")


def _require_text(value: object, field_name: str) -> str:
    """校验契约中的稳定文本身份字段。"""
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, field_name: str) -> str | None:
    """校验可选文本，避免将任意对象表示写入不可变契约。"""
    if value is None:
        return None
    return _require_text(value, field_name)


def _optional_error_type(value: object, field_name: str) -> str | None:
    """只接收稳定错误类型名，避免把异常正文作为 error_type 进入契约。"""
    if value is None:
        return None
    text = _require_text(value, field_name)
    if not _ERROR_TYPE_RE.fullmatch(text):
        raise ValueError(f"{field_name} must be an error type name")
    return text


def _nonnegative_int(value: object, field_name: str) -> int:
    """校验统计字段，显式拒绝布尔值伪装的整数。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _normalize_string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    """将契约集合规范化为稳定排序的不可变字符串元组。"""
    if not isinstance(value, (tuple, list, set, frozenset)):
        raise TypeError(f"{field_name} must be a collection of strings")
    normalized = {_require_text(item, field_name) for item in value}
    return tuple(sorted(normalized))


def freeze_safe_metadata(
    value: Mapping[str, object],
    *,
    field_name: str = "metadata",
) -> Mapping[str, object]:
    """深复制并冻结仅包含安全基础类型的有限元数据。"""
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    budget = _MetadataBudget(
        remaining_items=_MAX_METADATA_ITEMS,
        remaining_text_chars=_MAX_METADATA_TEXT_CHARS,
    )
    frozen = _freeze_safe_value(value, field_name, set(), budget, depth=0)
    assert isinstance(frozen, Mapping)
    return frozen


@dataclass(slots=True)
class _MetadataBudget:
    """限制 metadata 的总结构和文本规模，避免契约对象承载未界定载荷。"""

    remaining_items: int
    remaining_text_chars: int

    def consume_item(self, path: str) -> None:
        if self.remaining_items <= 0:
            raise ValueError(f"{path} exceeds the metadata item limit")
        self.remaining_items -= 1

    def consume_text(self, value: str, path: str) -> None:
        if len(value) > self.remaining_text_chars:
            raise ValueError(f"{path} exceeds the metadata text limit")
        self.remaining_text_chars -= len(value)


def _freeze_safe_value(
    value: object,
    path: str,
    ancestors: set[int],
    budget: _MetadataBudget,
    *,
    depth: int,
) -> object:
    """拒绝自定义对象、循环引用和非有限数字。"""
    if depth > _MAX_METADATA_DEPTH:
        raise ValueError(f"{path} exceeds the metadata nesting limit")
    if value is None or type(value) in (str, bool, int):
        if type(value) is str:
            budget.consume_text(value, path)
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} must not contain a non-finite number")
        return value

    if type(value) is dict or isinstance(value, MappingProxyType):
        value_id = id(value)
        if value_id in ancestors:
            raise ValueError(f"{path} must not contain cyclic containers")
        ancestors.add(value_id)
        try:
            frozen_mapping: dict[str, object] = {}
            for key, child in value.items():
                if not isinstance(key, str):
                    raise TypeError(f"{path} mapping keys must be strings")
                _validate_metadata_key(key, path)
                budget.consume_item(path)
                budget.consume_text(key, path)
                frozen_mapping[key] = _freeze_safe_value(
                    child,
                    f"{path}.{key}",
                    ancestors,
                    budget,
                    depth=depth + 1,
                )
            return MappingProxyType(frozen_mapping)
        finally:
            ancestors.remove(value_id)

    if type(value) in (list, tuple):
        value_id = id(value)
        if value_id in ancestors:
            raise ValueError(f"{path} must not contain cyclic containers")
        ancestors.add(value_id)
        try:
            frozen_values: list[object] = []
            for index, item in enumerate(value):
                budget.consume_item(path)
                frozen_values.append(
                    _freeze_safe_value(
                        item,
                        f"{path}[{index}]",
                        ancestors,
                        budget,
                        depth=depth + 1,
                    )
                )
            return tuple(frozen_values)
        finally:
            ancestors.remove(value_id)

    raise TypeError(f"{path} contains an unsupported value type")


def _validate_metadata_key(key: str, path: str) -> None:
    """仅允许摘要型 metadata 键，拒绝凭证和正文载荷的常见承载字段。"""
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    parts = frozenset(part for part in normalized.split("_") if part)
    if (
        normalized in _SENSITIVE_METADATA_KEYS
        or parts & _SENSITIVE_METADATA_KEY_PARTS
    ):
        raise ValueError(f"{path} contains a sensitive metadata key")
    if normalized in _DISALLOWED_METADATA_KEY_PARTS:
        raise ValueError(f"{path} contains a disallowed metadata key")


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """单个工具声明的不可执行、不可变能力摘要。"""

    name: str
    toolset: str
    description: str
    parameter_names: tuple[str, ...]
    required_parameters: tuple[str, ...]
    execution_environments: tuple[str, ...]
    default_enabled_environments: tuple[str, ...]
    unattended_allowed: bool
    approval_mode: str
    risk_level: str
    retry_safe: bool
    unknown_on_crash: bool
    supports_cancellation: bool
    has_status_check: bool

    def __post_init__(self) -> None:
        """复制集合字段，确保描述不持有 Registry 的可变 schema。"""
        object.__setattr__(self, "name", _require_text(self.name, "name"))
        object.__setattr__(self, "toolset", _require_text(self.toolset, "toolset"))
        if not isinstance(self.description, str):
            raise TypeError("description must be a string")
        object.__setattr__(
            self,
            "parameter_names",
            _normalize_string_tuple(self.parameter_names, "parameter_names"),
        )
        object.__setattr__(
            self,
            "required_parameters",
            _normalize_string_tuple(self.required_parameters, "required_parameters"),
        )
        object.__setattr__(
            self,
            "execution_environments",
            _normalize_string_tuple(
                self.execution_environments,
                "execution_environments",
            ),
        )
        object.__setattr__(
            self,
            "default_enabled_environments",
            _normalize_string_tuple(
                self.default_enabled_environments,
                "default_enabled_environments",
            ),
        )
        for field_name in (
            "unattended_allowed",
            "retry_safe",
            "unknown_on_crash",
            "supports_cancellation",
            "has_status_check",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(f"{field_name} must be a boolean")
        object.__setattr__(
            self,
            "approval_mode",
            _require_text(self.approval_mode, "approval_mode"),
        )
        object.__setattr__(
            self,
            "risk_level",
            _require_text(self.risk_level, "risk_level"),
        )


@dataclass(frozen=True, slots=True)
class ToolsetDescriptor:
    """按工具集聚合后的不可执行能力摘要。"""

    name: str
    tool_names: tuple[str, ...]
    execution_environments: tuple[str, ...]
    default_enabled_environments: tuple[str, ...]

    def __post_init__(self) -> None:
        """规范化所有聚合集合的排序和不可变性。"""
        object.__setattr__(self, "name", _require_text(self.name, "name"))
        object.__setattr__(
            self,
            "tool_names",
            _normalize_string_tuple(self.tool_names, "tool_names"),
        )
        object.__setattr__(
            self,
            "execution_environments",
            _normalize_string_tuple(
                self.execution_environments,
                "execution_environments",
            ),
        )
        object.__setattr__(
            self,
            "default_enabled_environments",
            _normalize_string_tuple(
                self.default_enabled_environments,
                "default_enabled_environments",
            ),
        )


@dataclass(frozen=True, slots=True)
class ToolCallObservation:
    """不含工具参数和结果的单次工具调用观察事件。"""

    observation_id: str
    run_id: str
    parent_run_id: str | None
    tool_call_id: str
    tool_name: str
    status: str
    success: bool
    error_type: str | None
    duration_ms: int

    def __post_init__(self) -> None:
        """校验 Hook 适配后的最小安全字段。"""
        for field_name in (
            "observation_id",
            "run_id",
            "tool_call_id",
            "tool_name",
            "status",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "parent_run_id",
            _optional_text(self.parent_run_id, "parent_run_id"),
        )
        object.__setattr__(
            self,
            "error_type",
            _optional_error_type(self.error_type, "error_type"),
        )
        if not isinstance(self.success, bool):
            raise TypeError("success must be a boolean")
        object.__setattr__(
            self,
            "duration_ms",
            _nonnegative_int(self.duration_ms, "duration_ms"),
        )


@dataclass(frozen=True, slots=True)
class ModelCallObservation:
    """不含 Prompt 或模型正文的单次模型调用观察事件。"""

    observation_id: str
    run_id: str
    parent_run_id: str | None
    finish_reason: str | None
    has_text: bool
    tool_call_count: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    duration_ms: int

    def __post_init__(self) -> None:
        """校验模型观察仅保存统计信息。"""
        object.__setattr__(
            self,
            "observation_id",
            _require_text(self.observation_id, "observation_id"),
        )
        object.__setattr__(self, "run_id", _require_text(self.run_id, "run_id"))
        for field_name in ("parent_run_id", "finish_reason"):
            object.__setattr__(
                self,
                field_name,
                _optional_text(getattr(self, field_name), field_name),
            )
        if not isinstance(self.has_text, bool):
            raise TypeError("has_text must be a boolean")
        for field_name in (
            "tool_call_count",
            "duration_ms",
        ):
            object.__setattr__(
                self,
                field_name,
                _nonnegative_int(getattr(self, field_name), field_name),
            )
        for field_name in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
        ):
            value = getattr(self, field_name)
            if value is not None:
                value = _nonnegative_int(value, field_name)
            object.__setattr__(self, field_name, value)


@dataclass(frozen=True, slots=True)
class RunObservation:
    """不含最终回复正文的一次运行结束观察事件。"""

    observation_id: str
    run_id: str
    parent_run_id: str | None
    status: str
    stop_reason: str
    iterations: int
    tool_call_count: int
    has_final_reply: bool

    def __post_init__(self) -> None:
        """校验运行事件的稳定关联和统计字段。"""
        for field_name in (
            "observation_id",
            "run_id",
            "status",
            "stop_reason",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "parent_run_id",
            _optional_text(self.parent_run_id, "parent_run_id"),
        )
        for field_name in ("iterations", "tool_call_count"):
            object.__setattr__(
                self,
                field_name,
                _nonnegative_int(getattr(self, field_name), field_name),
            )
        if not isinstance(self.has_final_reply, bool):
            raise TypeError("has_final_reply must be a boolean")


class ObservationSink(Protocol):
    """旁路观察事件的同步接收端。"""

    def record_tool_call(self, observation: ToolCallObservation) -> None:
        """接收单次工具调用观察。"""

    def record_model_call(self, observation: ModelCallObservation) -> None:
        """接收单次模型调用观察。"""

    def record_run_end(self, observation: RunObservation) -> None:
        """接收一次运行结束观察。"""


class NullObservationSink:
    """不保存状态且永不主动抛错的空观察接收端。"""

    __slots__ = ()

    def record_tool_call(self, observation: ToolCallObservation) -> None:
        del observation

    def record_model_call(self, observation: ModelCallObservation) -> None:
        del observation

    def record_run_end(self, observation: RunObservation) -> None:
        del observation
