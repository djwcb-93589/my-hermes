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


_SAFE_SCALAR_TYPES = (str, int, float, bool, type(None))


def _freeze_value(
    value: object,
    *,
    path: str,
    ancestors: set[int],
) -> object:
    """递归复制安全值，并将所有可变容器转换为不可变结构。"""
    if type(value) in _SAFE_SCALAR_TYPES:
        return value

    value_type = type(value)
    if value_type not in (dict, list, set, tuple, frozenset):
        raise TypeError(
            f"{path} contains unsupported value type: "
            f"{value_type.__name__}"
        )

    value_id = id(value)
    if value_id in ancestors:
        raise TypeError(f"{path} must not contain cyclic containers")
    ancestors.add(value_id)
    try:
        if value_type is dict:
            frozen_mapping: dict[str, object] = {}
            for key, nested_value in value.items():
                if type(key) is not str:
                    raise TypeError(f"{path} mapping keys must be strings")
                frozen_mapping[key] = _freeze_value(
                    nested_value,
                    path=f"{path}.{key}",
                    ancestors=ancestors,
                )
            return MappingProxyType(frozen_mapping)
        if value_type in (list, tuple):
            return tuple(
                _freeze_value(
                    nested_value,
                    path=f"{path}[{index}]",
                    ancestors=ancestors,
                )
                for index, nested_value in enumerate(value)
            )
        return frozenset(
            _freeze_value(
                nested_value,
                path=f"{path}{{item}}",
                ancestors=ancestors,
            )
            for nested_value in value
        )
    finally:
        ancestors.remove(value_id)


def _freeze_mapping(
    value: dict[str, object],
    *,
    field_name: str,
) -> Mapping[str, object]:
    """冻结顶层上下文映射，并拒绝非内建安全容器。"""
    if type(value) is not dict:
        raise TypeError(f"{field_name} must be a dict")
    frozen = _freeze_value(
        value,
        path=field_name,
        ancestors=set(),
    )
    assert isinstance(frozen, Mapping)
    return frozen


@dataclass(frozen=True, slots=True)
class HookContext:
    """传给 Hook 的只读上下文，只接受安全的递归基础数据。"""

    invocation_id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    payload: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """深度冻结输入，避免 Hook 修改调用方提供的任意嵌套容器。"""
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


@dataclass(frozen=True, slots=True, weakref_slot=True)
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
