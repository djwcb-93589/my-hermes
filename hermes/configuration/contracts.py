"""不依赖文件系统、YAML 或 Web 框架的配置管理契约。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, TypeAlias


MAX_CONFIG_PATCH_CHANGES = 32
MAX_CONFIG_STRING_LENGTH = 256
MAX_CONFIG_LIST_ITEMS = 64
MAX_CONFIG_LIST_ITEM_LENGTH = 128
MAX_CONFIG_INTEGER = (1 << 63) - 1
MAX_CONFIG_NUMBER = 1_000_000_000_000.0

_PUBLIC_NAME_PATTERN = re.compile(
    r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$"
)
_REVISION_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_SENSITIVE_SEGMENTS = frozenset({
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
})
_SENSITIVE_COMPOUNDS = frozenset({
    "access_key",
    "api_key",
    "auth_header",
    "client_key",
    "encryption_key",
    "private_key",
    "signing_key",
    "ssh_key",
})


ConfigValue: TypeAlias = (
    bool | int | float | str | tuple[str, ...] | None
)


class ConfigApplyMode(str, Enum):
    """配置文件修改后需要的显式应用方式。"""

    IMMEDIATE = "immediate"
    GATEWAY_RESTART = "gateway_restart"
    DASHBOARD_RESTART = "dashboard_restart"
    APPLICATION_RESTART = "application_restart"


class ConfigValueType(str, Enum):
    """首批配置中心允许出现的有限值类型。"""

    BOOLEAN = "boolean"
    INTEGER = "integer"
    NUMBER = "number"
    STRING = "string"
    STRING_LIST = "string_list"


class ConfigValueSource(str, Enum):
    """安全投影中允许公开的有限配置来源。"""

    FILE = "file"
    ENVIRONMENT = "environment"
    DEFAULT = "default"
    DERIVED = "derived"


class ConfigManagementError(Exception):
    """配置管理边界的稳定错误，不携带底层异常正文。"""

    reason_code = "config_unavailable"

    def __init__(self) -> None:
        super().__init__(self.reason_code)


class ConfigNotFound(ConfigManagementError):
    reason_code = "config_not_found"


class ConfigUnavailable(ConfigManagementError):
    reason_code = "config_unavailable"


class ConfigInvalid(ConfigManagementError):
    reason_code = "config_invalid"


class ConfigConflict(ConfigManagementError):
    reason_code = "config_conflict"


class ConfigFieldUnknown(ConfigManagementError):
    reason_code = "config_field_unknown"


class ConfigFieldReadOnly(ConfigManagementError):
    reason_code = "config_field_read_only"


class ConfigValueInvalid(ConfigManagementError):
    reason_code = "config_value_invalid"


class ConfigShadowed(ConfigManagementError):
    reason_code = "config_shadowed"


class ConfigWriteFailed(ConfigManagementError):
    reason_code = "config_write_failed"


def contains_sensitive_config_name(value: object) -> bool:
    """按字段组成部分识别凭证名称，显式注册也不能绕过。"""
    if type(value) is not str:
        return True
    normalized = value.strip().lower()
    if any(
        marker in normalized
        for marker in _SENSITIVE_COMPOUNDS | _SENSITIVE_SEGMENTS
    ):
        return True
    return False


@dataclass(frozen=True, slots=True)
class ConfigRevision:
    """由配置文件原始字节生成、不可逆且稳定的修订标识。"""

    value: str

    def __post_init__(self) -> None:
        if (
            type(self.value) is not str
            or not _REVISION_PATTERN.fullmatch(self.value)
        ):
            raise ValueError("config revision is invalid")


@dataclass(frozen=True, slots=True)
class ConfigFieldSpec:
    """一个可由配置中心识别的显式字段声明。"""

    public_name: str
    config_path: tuple[str, ...]
    value_type: ConfigValueType
    writable: bool
    sensitive: bool
    apply_mode: ConfigApplyMode
    nullable: bool = False
    has_default: bool = False
    default_value: ConfigValue = None
    description: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.public_name) is not str
            or not _PUBLIC_NAME_PATTERN.fullmatch(self.public_name)
        ):
            raise ValueError("config field public_name is invalid")
        if (
            type(self.config_path) is not tuple
            or not self.config_path
            or any(
                type(part) is not str
                or not part
                or not re.fullmatch(r"[a-z][a-z0-9_]*", part)
                for part in self.config_path
            )
        ):
            raise ValueError("config field path is invalid")
        if not isinstance(self.value_type, ConfigValueType):
            raise TypeError("config field value_type is invalid")
        if type(self.writable) is not bool or type(self.sensitive) is not bool:
            raise TypeError("config field security flags must be booleans")
        if not isinstance(self.apply_mode, ConfigApplyMode):
            raise TypeError("config field apply_mode is invalid")
        if type(self.nullable) is not bool or type(self.has_default) is not bool:
            raise TypeError("config field value flags must be booleans")
        path_name = ".".join(self.config_path)
        if (
            contains_sensitive_config_name(self.public_name)
            or contains_sensitive_config_name(path_name)
        ) and not self.sensitive:
            raise ValueError("sensitive config field must be marked sensitive")
        if self.sensitive and self.writable:
            raise ValueError("sensitive config field must be read-only")
        if self.description is not None and (
            type(self.description) is not str
            or not self.description.strip()
            or len(self.description) > 256
            or "\n" in self.description
            or "\r" in self.description
        ):
            raise ValueError("config field description is invalid")
        if self.has_default:
            object.__setattr__(
                self,
                "default_value",
                normalize_config_value(self, self.default_value),
            )


@dataclass(frozen=True, slots=True)
class ConfigStoredField:
    """Repository 返回的单字段安全状态，不包含配置路径。"""

    name: str
    source: ConfigValueSource
    configured: bool
    file_value: ConfigValue = None
    effective_value: ConfigValue = None

    def __post_init__(self) -> None:
        if (
            type(self.name) is not str
            or not _PUBLIC_NAME_PATTERN.fullmatch(self.name)
        ):
            raise ValueError("stored config field name is invalid")
        if not isinstance(self.source, ConfigValueSource):
            raise TypeError("stored config field source is invalid")
        if type(self.configured) is not bool:
            raise TypeError("stored config configured flag is invalid")
        if not _is_config_value(self.file_value) or not _is_config_value(
            self.effective_value
        ):
            raise TypeError("stored config field value is invalid")


@dataclass(frozen=True, slots=True)
class ConfigRepositorySnapshot:
    """Repository 的安全读取结果，仅包含注册字段。"""

    revision: ConfigRevision
    fields: tuple[ConfigStoredField, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.revision, ConfigRevision):
            raise TypeError("repository snapshot revision is invalid")
        if type(self.fields) is not tuple or any(
            not isinstance(field, ConfigStoredField) for field in self.fields
        ):
            raise TypeError("repository snapshot fields are invalid")


@dataclass(frozen=True, slots=True)
class ConfigFieldDescriptor:
    """面向调用方的单字段安全描述和当前值投影。"""

    name: str
    value_type: ConfigValueType
    writable: bool
    sensitive: bool
    apply_mode: ConfigApplyMode
    nullable: bool
    configured: bool
    source: ConfigValueSource | None = None
    file_value: ConfigValue = None
    effective_value: ConfigValue = None
    description: str | None = None

    def __post_init__(self) -> None:
        if type(self.name) is not str:
            raise TypeError("config descriptor name is invalid")
        if not isinstance(self.value_type, ConfigValueType):
            raise TypeError("config descriptor value_type is invalid")
        if (
            type(self.writable) is not bool
            or type(self.sensitive) is not bool
            or type(self.nullable) is not bool
            or type(self.configured) is not bool
        ):
            raise TypeError("config descriptor flags are invalid")
        if not isinstance(self.apply_mode, ConfigApplyMode):
            raise TypeError("config descriptor apply_mode is invalid")
        if self.sensitive and (
            self.writable
            or self.source is not None
            or self.file_value is not None
            or self.effective_value is not None
        ):
            raise ValueError("sensitive config descriptor exposes values")
        if not self.sensitive and not isinstance(
            self.source,
            ConfigValueSource,
        ):
            raise TypeError("ordinary config descriptor source is invalid")
        if not _is_config_value(self.file_value) or not _is_config_value(
            self.effective_value
        ):
            raise TypeError("config descriptor value is invalid")


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    """配置读取服务返回的不可变安全快照。"""

    revision: ConfigRevision
    fields: tuple[ConfigFieldDescriptor, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.revision, ConfigRevision):
            raise TypeError("config snapshot revision is invalid")
        if type(self.fields) is not tuple or any(
            not isinstance(field, ConfigFieldDescriptor)
            for field in self.fields
        ):
            raise TypeError("config snapshot fields are invalid")


@dataclass(frozen=True, slots=True)
class ConfigPatchChange:
    """一个按公共名称定位的配置修改，不接受调用方路径。"""

    name: str
    value: ConfigValue

    def __post_init__(self) -> None:
        if (
            type(self.name) is not str
            or not _PUBLIC_NAME_PATTERN.fullmatch(self.name)
        ):
            raise ValueError("config patch field name is invalid")
        value = self.value
        if type(value) is list:
            if any(type(item) is not str for item in value):
                raise ValueError("config patch list value is invalid")
            value = tuple(value)
            object.__setattr__(self, "value", value)
        if not _is_config_value(value):
            raise ValueError("config patch value is invalid")


@dataclass(frozen=True, slots=True)
class ConfigPatch:
    """携带乐观并发修订号的有限配置修改。"""

    expected_revision: ConfigRevision
    changes: tuple[ConfigPatchChange, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.expected_revision, ConfigRevision):
            raise TypeError("config patch revision is invalid")
        if (
            type(self.changes) is not tuple
            or not 1 <= len(self.changes) <= MAX_CONFIG_PATCH_CHANGES
            or any(
                not isinstance(change, ConfigPatchChange)
                for change in self.changes
            )
        ):
            raise ValueError("config patch changes are invalid")


@dataclass(frozen=True, slots=True)
class ConfigRepositoryWriteResult:
    """Repository 原子提交后的中立结果。"""

    previous_revision: ConfigRevision
    new_revision: ConfigRevision
    changed_fields: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(
            self.previous_revision,
            ConfigRevision,
        ) or not isinstance(self.new_revision, ConfigRevision):
            raise TypeError("config write result revision is invalid")
        if (
            type(self.changed_fields) is not tuple
            or len(self.changed_fields) > MAX_CONFIG_PATCH_CHANGES
            or len(set(self.changed_fields)) != len(self.changed_fields)
            or any(
                type(name) is not str
                or not _PUBLIC_NAME_PATTERN.fullmatch(name)
                for name in self.changed_fields
            )
        ):
            raise ValueError("config write result fields are invalid")
        if (not self.changed_fields) != (
            self.new_revision == self.previous_revision
        ):
            raise ValueError("config write result revision is inconsistent")


@dataclass(frozen=True, slots=True)
class ConfigPatchResult:
    """配置修改的安全结果，只提示应用方式而不执行重启。"""

    previous_revision: ConfigRevision
    new_revision: ConfigRevision
    changed_fields: tuple[str, ...]
    apply_modes: tuple[ConfigApplyMode, ...]
    restart_required: bool
    restart_targets: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(
            self.previous_revision,
            ConfigRevision,
        ) or not isinstance(self.new_revision, ConfigRevision):
            raise TypeError("config patch result revision is invalid")
        if (
            type(self.changed_fields) is not tuple
            or type(self.apply_modes) is not tuple
            or type(self.restart_targets) is not tuple
        ):
            raise TypeError("config patch result collections are invalid")
        if (
            len(self.changed_fields) > MAX_CONFIG_PATCH_CHANGES
            or len(set(self.changed_fields)) != len(self.changed_fields)
            or any(
                type(name) is not str
                or not _PUBLIC_NAME_PATTERN.fullmatch(name)
                for name in self.changed_fields
            )
            or len(set(self.apply_modes)) != len(self.apply_modes)
        ):
            raise ValueError("config patch result fields are invalid")
        if any(
            not isinstance(mode, ConfigApplyMode)
            for mode in self.apply_modes
        ):
            raise TypeError("config patch result apply mode is invalid")
        if type(self.restart_required) is not bool:
            raise TypeError("config patch result restart flag is invalid")
        allowed_targets = {"gateway", "dashboard", "application"}
        if (
            len(set(self.restart_targets)) != len(self.restart_targets)
            or any(
                type(target) is not str or target not in allowed_targets
                for target in self.restart_targets
            )
            or self.restart_required != bool(self.restart_targets)
        ):
            raise ValueError("config patch result restart targets are invalid")
        if (not self.changed_fields) != (
            self.new_revision == self.previous_revision
        ):
            raise ValueError("config patch result revision is inconsistent")


class ConfigRepository(Protocol):
    """配置服务依赖的文件无关 Repository 边界。"""

    def read_snapshot(self) -> ConfigRepositorySnapshot:
        """读取当前注册字段和修订号，不执行写入。"""

    def apply_patch(
        self,
        patch: ConfigPatch,
    ) -> ConfigRepositoryWriteResult:
        """重新核对修订号并原子应用已验证修改。"""


def _is_config_value(value: object) -> bool:
    if value is None or type(value) in (bool, int, float, str):
        return type(value) is not float or math.isfinite(value)
    return type(value) is tuple and all(
        type(item) is str for item in value
    )


def normalize_config_value(
    spec: ConfigFieldSpec,
    value: object,
) -> ConfigValue:
    """按字段声明执行严格、有限且不接受嵌套对象的类型校验。"""
    if not isinstance(spec, ConfigFieldSpec):
        raise TypeError("config field spec is invalid")
    if value is None:
        if spec.nullable:
            return None
        raise ValueError("config value cannot be null")
    if spec.value_type is ConfigValueType.BOOLEAN:
        if type(value) is not bool:
            raise ValueError("config value must be a boolean")
        return value
    if spec.value_type is ConfigValueType.INTEGER:
        if (
            type(value) is not int
            or value < -MAX_CONFIG_INTEGER
            or value > MAX_CONFIG_INTEGER
        ):
            raise ValueError("config value must be a bounded integer")
        return value
    if spec.value_type is ConfigValueType.NUMBER:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("config value must be a finite number")
        normalized_number = float(value)
        if (
            not math.isfinite(normalized_number)
            or abs(normalized_number) > MAX_CONFIG_NUMBER
        ):
            raise ValueError("config value must be a bounded finite number")
        return normalized_number
    if spec.value_type is ConfigValueType.STRING:
        if (
            type(value) is not str
            or not value
            or value != value.strip()
            or len(value) > MAX_CONFIG_STRING_LENGTH
            or "\n" in value
            or "\r" in value
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("config value must be a compact string")
        return value
    if spec.value_type is ConfigValueType.STRING_LIST:
        if type(value) not in (list, tuple) or len(value) > MAX_CONFIG_LIST_ITEMS:
            raise ValueError("config value must be a bounded string list")
        normalized_items: list[str] = []
        for item in value:
            if (
                type(item) is not str
                or not item
                or item != item.strip()
                or len(item) > MAX_CONFIG_LIST_ITEM_LENGTH
                or "\n" in item
                or "\r" in item
                or any(ord(character) < 32 for character in item)
            ):
                raise ValueError("config list item must be a compact string")
            normalized_items.append(item)
        return tuple(normalized_items)
    raise TypeError("config value type is unsupported")


__all__ = [
    "MAX_CONFIG_PATCH_CHANGES",
    "ConfigApplyMode",
    "ConfigConflict",
    "ConfigFieldDescriptor",
    "ConfigFieldReadOnly",
    "ConfigFieldSpec",
    "ConfigFieldUnknown",
    "ConfigInvalid",
    "ConfigManagementError",
    "ConfigNotFound",
    "ConfigPatch",
    "ConfigPatchChange",
    "ConfigPatchResult",
    "ConfigRepository",
    "ConfigRepositorySnapshot",
    "ConfigRepositoryWriteResult",
    "ConfigRevision",
    "ConfigShadowed",
    "ConfigSnapshot",
    "ConfigStoredField",
    "ConfigUnavailable",
    "ConfigValue",
    "ConfigValueInvalid",
    "ConfigValueSource",
    "ConfigValueType",
    "ConfigWriteFailed",
    "contains_sensitive_config_name",
    "normalize_config_value",
]
