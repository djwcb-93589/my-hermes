"""隔离 Agent Runtime 的不可变输入与输出契约。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol


def _freeze_value(value: object) -> object:
    """递归复制并冻结常见容器，切断调用方对执行边界的后续修改。"""

    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                deepcopy(key): _freeze_value(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_value(item) for item in value)
    return deepcopy(value)


def _plain_value_sort_key(value: object) -> tuple[object, ...]:
    """为普通数据生成稳定结构键；未知对象明确拒绝隐式字符串化。"""

    if value is None:
        return ("builtins.NoneType", "")
    if type(value) in (bool, int, float, str, bytes):
        value_type = type(value)
        return (
            f"{value_type.__module__}.{value_type.__qualname__}",
            value,
        )
    if isinstance(value, Mapping):
        entries = tuple(sorted(
            (
                _plain_value_sort_key(key),
                _plain_value_sort_key(item),
            )
            for key, item in value.items()
        ))
        return ("mapping", entries)
    if isinstance(value, (tuple, list)):
        return (
            "sequence",
            tuple(_plain_value_sort_key(item) for item in value),
        )
    if isinstance(value, (frozenset, set)):
        return (
            "set",
            tuple(sorted(
                _plain_value_sort_key(item)
                for item in value
            )),
        )
    raise TypeError("set item does not have a stable plain-data order")


def _to_plain_value(value: object) -> object:
    """递归导出独立普通容器，不保留内部不可变容器引用。"""

    if isinstance(value, Mapping):
        plain_mapping: dict[object, object] = {}
        for key, item in value.items():
            plain_key = _to_plain_value(key)
            plain_item = _to_plain_value(item)
            try:
                plain_mapping[plain_key] = plain_item
            except TypeError as exc:
                raise TypeError(
                    "mapping key cannot be exported as a plain dict key"
                ) from exc
        return plain_mapping
    if isinstance(value, (tuple, list)):
        return [_to_plain_value(item) for item in value]
    if isinstance(value, (frozenset, set)):
        items = [_to_plain_value(item) for item in value]
        return sorted(items, key=_plain_value_sort_key)
    return deepcopy(value)


def _freeze_mapping(
    value: Mapping[str, object],
    *,
    field_name: str,
) -> Mapping[str, object]:
    """复制并冻结 Mapping，同时保留稳定的字段错误。"""

    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    frozen = _freeze_value(value)
    if not isinstance(frozen, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    return frozen


class IsolatedAgentSessionInitializer(Protocol):
    """在 Executor 已接管 Session 后执行一次通用初始化。"""

    def __call__(self, *, session_key: str) -> None:
        """仅按可信 session_key 初始化可被统一清理的资源。"""


@dataclass(frozen=True, slots=True)
class IsolatedAgentRunSpec:
    """调用方已经完成 Prompt 与工具策略解析后的隔离执行计划。"""

    session_key: str
    goal: str
    system_prompt: str
    model: str
    max_iterations: int
    tool_definitions: tuple[Mapping[str, object], ...]
    allowed_tool_names: frozenset[str]
    model_kwargs: Mapping[str, object]

    def __post_init__(self) -> None:
        """防御性复制所有集合与 Mapping，运行期间不再信任外部引用。"""

        object.__setattr__(
            self,
            "tool_definitions",
            tuple(
                _freeze_mapping(
                    definition,
                    field_name="tool_definitions item",
                )
                for definition in self.tool_definitions
            ),
        )
        object.__setattr__(
            self,
            "allowed_tool_names",
            frozenset(self.allowed_tool_names),
        )
        object.__setattr__(
            self,
            "model_kwargs",
            _freeze_mapping(
                self.model_kwargs,
                field_name="model_kwargs",
            ),
        )

    def to_dict(self) -> dict[str, object]:
        """导出适合序列化或传输的全新普通 Python 数据。"""

        plain = _to_plain_value(
            {
                "session_key": self.session_key,
                "goal": self.goal,
                "system_prompt": self.system_prompt,
                "model": self.model,
                "max_iterations": self.max_iterations,
                "tool_definitions": self.tool_definitions,
                "allowed_tool_names": self.allowed_tool_names,
                "model_kwargs": self.model_kwargs,
            }
        )
        if not isinstance(plain, dict):
            raise TypeError("spec export must produce a dict")
        return plain


@dataclass(frozen=True, slots=True)
class IsolatedAgentRunResult:
    """与 AgentLoop 内部对象解耦的隔离运行完整结果。"""

    ok: bool
    status: str
    summary: str
    messages: tuple[Mapping[str, object], ...]
    iterations: int
    tools_used: tuple[str, ...]
    error: str | None
    error_type: str | None
    fatal: bool
    retryable: bool
    approval_request: Mapping[str, object] | None
    tool_batches: int
    tool_call_count: int

    def __post_init__(self) -> None:
        """冻结 AgentLoop 返回的可变集合，避免跨层共享内部状态。"""

        object.__setattr__(
            self,
            "messages",
            tuple(
                _freeze_mapping(message, field_name="messages item")
                for message in self.messages
            ),
        )
        object.__setattr__(
            self,
            "tools_used",
            tuple(self.tools_used),
        )
        if self.approval_request is not None:
            object.__setattr__(
                self,
                "approval_request",
                _freeze_mapping(
                    self.approval_request,
                    field_name="approval_request",
                ),
            )

    def to_dict(self) -> dict[str, object]:
        """导出不共享内部容器引用的完整普通结果。"""

        plain = _to_plain_value(
            {
                "ok": self.ok,
                "status": self.status,
                "summary": self.summary,
                "messages": self.messages,
                "iterations": self.iterations,
                "tools_used": self.tools_used,
                "error": self.error,
                "error_type": self.error_type,
                "fatal": self.fatal,
                "retryable": self.retryable,
                "approval_request": self.approval_request,
                "tool_batches": self.tool_batches,
                "tool_call_count": self.tool_call_count,
            }
        )
        if not isinstance(plain, dict):
            raise TypeError("result export must produce a dict")
        return plain


__all__ = [
    "IsolatedAgentSessionInitializer",
    "IsolatedAgentRunResult",
    "IsolatedAgentRunSpec",
]
