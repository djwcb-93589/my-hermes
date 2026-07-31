"""不依赖 Dashboard 或运行时组件的不可变配置环境快照。"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


_ENVIRONMENT_REFERENCE_PATTERN = re.compile(r"\$\{(\w+)\}")
_ENVIRONMENT_KEY_PATTERN = re.compile(r"\w+\Z")
_MAX_ENVIRONMENT_KEYS = 512
_MAX_ENVIRONMENT_SCAN_DEPTH = 32


@dataclass(frozen=True, slots=True)
class ConfigEnvironment:
    """只保存一次配置解析明确允许使用的环境键值。"""

    _values: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self._values, Mapping):
            raise TypeError("config environment values must be a mapping")
        copied: dict[str, str] = {}
        for key, value in self._values.items():
            if not is_config_environment_key(key) or type(value) is not str:
                raise ValueError("config environment contains an invalid entry")
            copied[key] = value
        if len(copied) > _MAX_ENVIRONMENT_KEYS:
            raise ValueError("config environment contains too many entries")
        object.__setattr__(self, "_values", MappingProxyType(copied))

    @classmethod
    def empty(cls) -> ConfigEnvironment:
        """创建不允许解析任何环境键的空快照。"""
        return cls({})

    @classmethod
    def from_sources(
        cls,
        *,
        allowed_keys: Iterable[str],
        process_environment: Mapping[str, str],
        profile_environment: Mapping[str, str],
    ) -> ConfigEnvironment:
        """按进程环境优先、profile 次之构造有限快照。"""
        if not isinstance(process_environment, Mapping) or not isinstance(
            profile_environment,
            Mapping,
        ):
            raise TypeError("config environment sources must be mappings")
        normalized_keys = _environment_keys(allowed_keys)
        values: dict[str, str] = {}
        for key in normalized_keys:
            if key in process_environment:
                value = process_environment[key]
            elif key in profile_environment:
                value = profile_environment[key]
            else:
                continue
            if type(value) is not str:
                raise ValueError("config environment value must be a string")
            values[key] = value
        return cls(values)

    @property
    def source_present(self) -> bool:
        """是否至少有一个允许键来自进程环境或 profile。"""
        return bool(self._values)

    def contains(self, key: str) -> bool:
        """判断允许键是否在快照中存在，空字符串仍视为存在。"""
        _require_environment_key(key)
        return key in self._values

    def get(self, key: str) -> str | None:
        """读取一个允许键；调用方不得把结果写入 API 或日志。"""
        _require_environment_key(key)
        return self._values.get(key)

    def first_nonempty(
        self,
        keys: tuple[str, ...],
    ) -> str | None:
        """按声明顺序返回首个非空值，保持运行时 ``or`` 语义。"""
        if type(keys) is not tuple:
            raise TypeError("environment override keys must be a tuple")
        for key in keys:
            value = self.get(key)
            if value:
                return value
        return None

    def expand(self, value: object) -> object:
        """使用固定快照递归展开 ``${VAR}``，不访问全局环境。"""
        return _expand_value(value, self, depth=0, active=set())


def is_config_environment_key(value: object) -> bool:
    """判断字符串是否符合现有 ``${VAR}`` 展开允许的键格式。"""
    return (
        type(value) is str
        and bool(value)
        and _ENVIRONMENT_KEY_PATTERN.fullmatch(value) is not None
    )


def environment_reference_keys(value: object) -> tuple[str, ...]:
    """按稳定顺序收集配置文档实际引用的有限环境键。"""
    found: dict[str, None] = {}
    _collect_reference_keys(value, found, depth=0, active=set())
    if len(found) > _MAX_ENVIRONMENT_KEYS:
        raise ValueError("config contains too many environment references")
    return tuple(found)


def _environment_keys(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise TypeError("allowed environment keys must be an iterable")
    normalized: dict[str, None] = {}
    for value in values:
        _require_environment_key(value)
        normalized[value] = None
        if len(normalized) > _MAX_ENVIRONMENT_KEYS:
            raise ValueError("too many allowed environment keys")
    return tuple(normalized)


def _require_environment_key(value: object) -> None:
    if not is_config_environment_key(value):
        raise ValueError("config environment key is invalid")


def _replace_references(value: str, environment: ConfigEnvironment) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        replacement = environment.get(key)
        return match.group(0) if replacement is None else replacement

    return _ENVIRONMENT_REFERENCE_PATTERN.sub(replace, value)


def _expand_value(
    value: object,
    environment: ConfigEnvironment,
    *,
    depth: int,
    active: set[int],
) -> object:
    if depth > _MAX_ENVIRONMENT_SCAN_DEPTH:
        raise ValueError("config environment expansion is too deeply nested")
    if type(value) is str:
        return _replace_references(value, environment)
    if type(value) not in (dict, list, tuple):
        return value
    identity = id(value)
    if identity in active:
        raise ValueError("config environment expansion contains a cycle")
    active.add(identity)
    try:
        if type(value) is dict:
            return {
                key: _expand_value(
                    item,
                    environment,
                    depth=depth + 1,
                    active=active,
                )
                for key, item in value.items()
            }
        expanded = tuple(
            _expand_value(
                item,
                environment,
                depth=depth + 1,
                active=active,
            )
            for item in value
        )
        return list(expanded) if type(value) is list else expanded
    finally:
        active.remove(identity)


def _collect_reference_keys(
    value: object,
    found: dict[str, None],
    *,
    depth: int,
    active: set[int],
) -> None:
    if depth > _MAX_ENVIRONMENT_SCAN_DEPTH:
        raise ValueError("config environment references are too deeply nested")
    if type(value) is str:
        for match in _ENVIRONMENT_REFERENCE_PATTERN.finditer(value):
            found.setdefault(match.group(1), None)
        return
    if type(value) not in (dict, list, tuple):
        return
    identity = id(value)
    if identity in active:
        raise ValueError("config environment references contain a cycle")
    active.add(identity)
    try:
        items = value.values() if type(value) is dict else value
        for item in items:
            _collect_reference_keys(
                item,
                found,
                depth=depth + 1,
                active=active,
            )
    finally:
        active.remove(identity)


__all__ = [
    "ConfigEnvironment",
    "environment_reference_keys",
    "is_config_environment_key",
]
