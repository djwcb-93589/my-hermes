"""隔离 Agent Runtime 的不可变输入与输出契约。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType


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


__all__ = [
    "IsolatedAgentRunResult",
    "IsolatedAgentRunSpec",
]
