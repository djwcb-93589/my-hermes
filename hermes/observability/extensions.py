"""特殊 Dashboard 交互的声明式扩展契约。"""

from __future__ import annotations

from dataclasses import dataclass


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list, set, frozenset)):
        raise TypeError(f"{field_name} must be a collection of strings")
    return tuple(sorted({_required_text(item, field_name) for item in value}))


@dataclass(frozen=True, slots=True)
class DashboardExtensionDescriptor:
    """不绑定 Router、组件或处理器的特殊交互声明。"""

    extension_id: str
    resource_kind: str
    display_name: str
    capability_names: tuple[str, ...]
    interaction_kinds: tuple[str, ...]
    schema_version: int

    def __post_init__(self) -> None:
        """规范化稳定身份和不可变集合字段。"""
        for field_name in ("extension_id", "resource_kind", "display_name"):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "capability_names",
            _string_tuple(self.capability_names, "capability_names"),
        )
        object.__setattr__(
            self,
            "interaction_kinds",
            _string_tuple(self.interaction_kinds, "interaction_kinds"),
        )
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version <= 0
        ):
            raise ValueError("schema_version must be a positive integer")


class DashboardExtensionRegistry:
    """仅保存不可变 Descriptor，禁止覆盖或执行 Extension。"""

    def __init__(self) -> None:
        self._descriptors: dict[str, DashboardExtensionDescriptor] = {}

    def register(self, descriptor: DashboardExtensionDescriptor) -> None:
        """注册唯一扩展标识；重复注册显式失败。"""
        if not isinstance(descriptor, DashboardExtensionDescriptor):
            raise TypeError("descriptor must be a DashboardExtensionDescriptor")
        if descriptor.extension_id in self._descriptors:
            raise ValueError("dashboard extension is already registered")
        self._descriptors[descriptor.extension_id] = descriptor

    def get(self, extension_id: str) -> DashboardExtensionDescriptor | None:
        """按唯一标识返回不可变扩展快照。"""
        if not isinstance(extension_id, str):
            raise TypeError("extension_id must be a string")
        return self._descriptors.get(extension_id)

    def descriptors(self) -> tuple[DashboardExtensionDescriptor, ...]:
        """按 extension_id 稳定排序返回全部已注册声明。"""
        return tuple(
            self._descriptors[extension_id]
            for extension_id in sorted(self._descriptors)
        )
