"""Hook 基础设施的稳定公共契约。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


HookName = str
"""用于注册和分发 Hook 的事件名称。"""


def normalize_hook_name(value: object) -> HookName:
    """校验并规范化 Hook 事件名称。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("hook event name must be a non-empty string")
    return value.strip()


def _freeze_mapping(
    value: Mapping[str, object],
    *,
    field_name: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class HookContext:
    """传给 Hook 的只读、与业务实现解耦的上下文数据。"""

    invocation_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """复制映射，避免 Hook 通过上下文修改调用方的顶层数据。"""
        if self.invocation_id is not None and not isinstance(
            self.invocation_id, str
        ):
            raise TypeError("invocation_id must be a string or None")
        object.__setattr__(
            self,
            "metadata",
            _freeze_mapping(self.metadata, field_name="metadata"),
        )
        object.__setattr__(
            self,
            "payload",
            _freeze_mapping(self.payload, field_name="payload"),
        )


@dataclass(frozen=True, slots=True)
class HookEvent:
    """一次 Hook 分发所需的事件名称和独立上下文。"""

    name: HookName
    context: HookContext

    def __post_init__(self) -> None:
        """在事件创建时完成公共输入校验。"""
        object.__setattr__(self, "name", normalize_hook_name(self.name))
        if not isinstance(self.context, HookContext):
            raise TypeError("context must be a HookContext")


HookCallback = Callable[[HookContext], object]
"""同步或异步 Hook 回调的统一调用形状。"""


@dataclass(frozen=True, slots=True)
class HookRegistration:
    """已注册 Hook 的不可变描述。"""

    event_name: HookName
    hook_id: str
    callback: HookCallback = field(repr=False, compare=False)
    timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class HookInvocationResult:
    """单个 Hook 的结构化执行结果，不暴露异常对象。"""

    hook_id: str
    success: bool
    value: object | None = None
    error_type: str | None = None
    timed_out: bool = False
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class HookDispatchResult:
    """一次事件分发的完整结果，按 Hook 注册顺序保存。"""

    event: HookEvent
    results: tuple[HookInvocationResult, ...]


class HookRegistrationError(ValueError):
    """Hook 注册输入无效或与既有注册冲突时抛出。"""

