"""配置中心的 YAML 文件 Repository 实现。"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import logging
import os
import re
import stat
import tempfile
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from hermes.config_model import validate_config_mapping

from .contracts import (
    MAX_CONFIG_PATCH_CHANGES,
    ConfigConflict,
    ConfigFieldReadOnly,
    ConfigFieldSpec,
    ConfigFieldUnknown,
    ConfigInvalid,
    ConfigManagementError,
    ConfigNotFound,
    ConfigPatch,
    ConfigPatchChange,
    ConfigRepositorySnapshot,
    ConfigRepositoryWriteResult,
    ConfigRevision,
    ConfigShadowed,
    ConfigStoredField,
    ConfigUnavailable,
    ConfigValueInvalid,
    ConfigValueSource,
    ConfigWriteFailed,
    normalize_config_value,
)
from .fields import ConfigFieldRegistry


logger = logging.getLogger(__name__)

_MAX_CONFIG_BYTES = 4 * 1024 * 1024
_ENVIRONMENT_PLACEHOLDER_PATTERN = re.compile(
    r"\$\{[A-Za-z_][A-Za-z0-9_]*\}"
)


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """拒绝同一 Mapping 中重复键的安全 YAML Loader。"""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    if not isinstance(node, MappingNode):
        raise ConstructorError(
            None,
            None,
            "expected a mapping node",
            node.start_mark,
        )
    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found a duplicate key",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True, slots=True)
class _ReadDocument:
    raw_mapping: dict[object, object]
    validated_mapping: dict
    revision: ConfigRevision
    mode: int


class YamlConfigRepository:
    """只负责安全读取、完整校验和原子写入配置文件。"""

    __slots__ = ("_path", "_process_lock", "_registry")

    def __init__(
        self,
        path: str | os.PathLike[str],
        registry: ConfigFieldRegistry,
    ) -> None:
        try:
            normalized_path = Path(path)
        except (TypeError, ValueError) as exc:
            raise TypeError("config path is invalid") from exc
        if not isinstance(registry, ConfigFieldRegistry):
            raise TypeError("registry must be a ConfigFieldRegistry")
        self._path = normalized_path
        self._registry = registry
        self._process_lock = threading.RLock()

    def read_snapshot(self) -> ConfigRepositorySnapshot:
        """读取一次稳定的原始文件，并只投影注册字段。"""
        try:
            document = self._read_document()
            return self._snapshot_from_document(document)
        except ConfigManagementError as exc:
            _log_operation_failure("read", exc)
            raise
        except Exception as exc:
            _log_operation_failure("read", exc)
            raise ConfigUnavailable() from exc

    def apply_patch(
        self,
        patch: ConfigPatch,
    ) -> ConfigRepositoryWriteResult:
        """在进程锁和跨进程文件锁内完成乐观并发原子修改。"""
        changed_count = 0
        apply_mode = "none"
        try:
            self._validate_patch_shape(patch)
            # 在创建伴生锁文件前先拒绝缺失或不安全的配置目标。
            _inspect_existing_regular_file(self._path)
            with self._process_lock, _configuration_file_lock(self._path):
                document = self._read_document()
                if document.revision != patch.expected_revision:
                    raise ConfigConflict()

                stored_by_name = {
                    field.name: field
                    for field in self._project_fields(document)
                }
                updated = copy.deepcopy(document.raw_mapping)
                changed_names: list[str] = []
                changed_specs = []
                seen: set[str] = set()

                for change in patch.changes:
                    if change.name in seen:
                        raise ConfigValueInvalid()
                    seen.add(change.name)
                    spec = self._registry.get(change.name)
                    if spec is None:
                        raise ConfigFieldUnknown()
                    if not spec.writable or spec.sensitive:
                        raise ConfigFieldReadOnly()
                    stored = stored_by_name.get(spec.public_name)
                    if stored is None:
                        raise ConfigUnavailable()
                    if stored.source is ConfigValueSource.ENVIRONMENT:
                        raise ConfigShadowed()
                    try:
                        normalized_value = normalize_config_value(
                            spec,
                            change.value,
                        )
                    except (TypeError, ValueError) as exc:
                        raise ConfigValueInvalid() from exc

                    if normalized_value == stored.effective_value:
                        continue
                    updated = _mapping_with_path_value(
                        updated,
                        spec.config_path,
                        _yaml_compatible_value(normalized_value),
                    )
                    changed_names.append(spec.public_name)
                    changed_specs.append(spec)

                changed_count = len(changed_names)
                apply_mode = _apply_mode_summary(changed_specs)
                if not changed_names:
                    self._assert_revision_unchanged(document.revision)
                    _log_operation_success(0, "none")
                    return ConfigRepositoryWriteResult(
                        previous_revision=document.revision,
                        new_revision=document.revision,
                        changed_fields=(),
                    )

                _validate_complete_mapping(updated)
                serialized = _serialize_mapping(updated)
                # 对最终将要写入的字节再次解析和正式校验。
                _parse_and_validate(serialized)
                temp_path = self._write_temporary(
                    serialized,
                    mode=document.mode,
                )
                try:
                    latest_mode = self._assert_revision_unchanged(
                        document.revision
                    )
                    _apply_file_mode(temp_path, latest_mode)
                    os.replace(temp_path, self._path)
                    temp_path = None
                    _fsync_parent_best_effort(self._path.parent)
                except ConfigManagementError:
                    raise
                except OSError as exc:
                    raise ConfigWriteFailed() from exc
                finally:
                    _remove_temporary_best_effort(temp_path)

                new_revision = _revision_for_bytes(serialized)
                _log_operation_success(changed_count, apply_mode)
                return ConfigRepositoryWriteResult(
                    previous_revision=document.revision,
                    new_revision=new_revision,
                    changed_fields=tuple(changed_names),
                )
        except ConfigManagementError as exc:
            _log_operation_failure(
                "write",
                exc,
                changed_count=changed_count,
                apply_mode=apply_mode,
            )
            raise
        except Exception as exc:
            _log_operation_failure(
                "write",
                exc,
                changed_count=changed_count,
                apply_mode=apply_mode,
            )
            raise ConfigWriteFailed() from exc

    def _validate_patch_shape(self, patch: ConfigPatch) -> None:
        if not isinstance(patch, ConfigPatch):
            raise ConfigValueInvalid()
        if (
            not isinstance(patch.expected_revision, ConfigRevision)
            or type(patch.changes) is not tuple
            or not 1 <= len(patch.changes) <= MAX_CONFIG_PATCH_CHANGES
        ):
            raise ConfigValueInvalid()
        for change in patch.changes:
            if (
                not isinstance(change, ConfigPatchChange)
                or type(change.name) is not str
            ):
                raise ConfigValueInvalid()

    def _read_document(self) -> _ReadDocument:
        raw_bytes, mode = _read_regular_file(self._path)
        raw_mapping, validated = _parse_and_validate(raw_bytes)
        return _ReadDocument(
            raw_mapping=raw_mapping,
            validated_mapping=validated,
            revision=_revision_for_bytes(raw_bytes),
            mode=mode,
        )

    def _snapshot_from_document(
        self,
        document: _ReadDocument,
    ) -> ConfigRepositorySnapshot:
        return ConfigRepositorySnapshot(
            revision=document.revision,
            fields=self._project_fields(document),
        )

    def _project_fields(
        self,
        document: _ReadDocument,
    ) -> tuple[ConfigStoredField, ...]:
        projected: list[ConfigStoredField] = []
        for spec in self._registry.fields:
            file_present, raw_value = _mapping_path(
                document.raw_mapping,
                spec.config_path,
            )
            effective_present, effective_value = _mapping_path(
                document.validated_mapping,
                spec.config_path,
            )
            environment_resolved = (
                file_present
                and _contains_environment_placeholder(raw_value)
                and raw_value != effective_value
            )

            if spec.sensitive:
                source = _value_source(
                    file_present=file_present,
                    environment_resolved=environment_resolved,
                    has_default=spec.has_default,
                )
                configured = _sensitive_value_configured(
                    raw_value,
                    effective_value,
                    file_present=file_present,
                    effective_present=effective_present,
                    environment_resolved=environment_resolved,
                )
                projected.append(
                    ConfigStoredField(
                        name=spec.public_name,
                        source=source,
                        configured=configured,
                    )
                )
                continue

            if environment_resolved:
                projected.append(
                    ConfigStoredField(
                        name=spec.public_name,
                        source=ConfigValueSource.ENVIRONMENT,
                        configured=True,
                    )
                )
                continue

            if not effective_present:
                raise ConfigInvalid()
            try:
                normalized_effective = normalize_config_value(
                    spec,
                    effective_value,
                )
            except (TypeError, ValueError) as exc:
                raise ConfigInvalid() from exc
            source = _value_source(
                file_present=file_present,
                environment_resolved=False,
                has_default=spec.has_default,
            )
            file_value = (
                normalized_effective
                if source is ConfigValueSource.FILE
                else None
            )
            projected.append(
                ConfigStoredField(
                    name=spec.public_name,
                    source=source,
                    configured=file_present or spec.has_default,
                    file_value=file_value,
                    effective_value=normalized_effective,
                )
            )
        return tuple(projected)

    def _assert_revision_unchanged(
        self,
        expected_revision: ConfigRevision,
    ) -> int:
        raw_bytes, mode = _read_regular_file(self._path)
        if _revision_for_bytes(raw_bytes) != expected_revision:
            raise ConfigConflict()
        return mode

    def _write_temporary(self, payload: bytes, *, mode: int) -> Path:
        parent = self._path.parent
        descriptor: int | None = None
        temp_name: str | None = None
        try:
            descriptor, temp_name = tempfile.mkstemp(
                dir=parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
            )
            _apply_descriptor_mode(descriptor, mode)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            return Path(temp_name)
        except ConfigManagementError:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            _remove_temporary_best_effort(
                Path(temp_name) if temp_name is not None else None
            )
            raise
        except OSError as exc:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            _remove_temporary_best_effort(
                Path(temp_name) if temp_name is not None else None
            )
            raise ConfigWriteFailed() from exc


def _inspect_existing_regular_file(path: Path) -> os.stat_result:
    try:
        identity = path.lstat()
    except FileNotFoundError as exc:
        raise ConfigNotFound() from exc
    except OSError as exc:
        raise ConfigUnavailable() from exc
    if stat.S_ISLNK(identity.st_mode) or not stat.S_ISREG(identity.st_mode):
        raise ConfigUnavailable()
    return identity


def _read_regular_file(path: Path) -> tuple[bytes, int]:
    identity = _inspect_existing_regular_file(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise ConfigNotFound() from exc
    except OSError as exc:
        raise ConfigUnavailable() from exc
    try:
        opened_identity = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_identity.st_mode)
            or not os.path.samestat(identity, opened_identity)
        ):
            raise ConfigUnavailable()
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(_MAX_CONFIG_BYTES + 1)
        if len(payload) > _MAX_CONFIG_BYTES:
            raise ConfigUnavailable()
        try:
            current_identity = path.lstat()
        except FileNotFoundError as exc:
            raise ConfigUnavailable() from exc
        except OSError as exc:
            raise ConfigUnavailable() from exc
        if (
            stat.S_ISLNK(current_identity.st_mode)
            or not os.path.samestat(opened_identity, current_identity)
        ):
            raise ConfigUnavailable()
        return payload, stat.S_IMODE(opened_identity.st_mode)
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _parse_and_validate(
    raw_bytes: bytes,
) -> tuple[dict[object, object], dict]:
    try:
        raw_text = raw_bytes.decode("utf-8-sig")
        loaded = yaml.load(raw_text, Loader=_UniqueKeySafeLoader)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise ConfigInvalid() from exc
    if not isinstance(loaded, dict):
        raise ConfigInvalid()
    try:
        raw_mapping = copy.deepcopy(loaded)
        validated = validate_config_mapping(
            copy.deepcopy(raw_mapping),
            expand_environment=True,
        )
    except ConfigManagementError:
        raise
    except Exception as exc:
        raise ConfigInvalid() from exc
    if not isinstance(validated, dict):
        raise ConfigInvalid()
    return raw_mapping, validated


def _validate_complete_mapping(raw: Mapping[object, object]) -> None:
    try:
        validate_config_mapping(
            copy.deepcopy(raw),
            expand_environment=True,
        )
    except Exception as exc:
        raise ConfigInvalid() from exc


def _serialize_mapping(raw: Mapping[object, object]) -> bytes:
    try:
        text = yaml.safe_dump(
            dict(raw),
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        return text.encode("utf-8")
    except (TypeError, ValueError, yaml.YAMLError) as exc:
        raise ConfigWriteFailed() from exc


def _revision_for_bytes(payload: bytes) -> ConfigRevision:
    return ConfigRevision(
        value=f"sha256:{hashlib.sha256(payload).hexdigest()}"
    )


def _mapping_path(
    mapping: Mapping[object, object],
    path: tuple[str, ...],
) -> tuple[bool, object]:
    current: object = mapping
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _mapping_with_path_value(
    mapping: dict[object, object],
    path: tuple[str, ...],
    value: object,
) -> dict[object, object]:
    """沿修改路径逐层复制 Mapping，避免 YAML alias 连带变化。"""
    if not path:
        raise ConfigInvalid()
    copied = dict(mapping)
    part = path[0]
    if len(path) == 1:
        copied[part] = value
        return copied
    child = mapping.get(part)
    if child is None:
        child = {}
    if not isinstance(child, dict):
        raise ConfigInvalid()
    copied[part] = _mapping_with_path_value(child, path[1:], value)
    return copied


def _yaml_compatible_value(value: object) -> object:
    if type(value) is tuple:
        return list(value)
    return value


def _contains_environment_placeholder(value: object) -> bool:
    if type(value) is str:
        return _ENVIRONMENT_PLACEHOLDER_PATTERN.search(value) is not None
    if isinstance(value, Mapping):
        return any(
            _contains_environment_placeholder(item)
            for item in value.values()
        )
    if type(value) in (list, tuple):
        return any(_contains_environment_placeholder(item) for item in value)
    return False


def _value_source(
    *,
    file_present: bool,
    environment_resolved: bool,
    has_default: bool,
) -> ConfigValueSource:
    if environment_resolved:
        return ConfigValueSource.ENVIRONMENT
    if file_present:
        return ConfigValueSource.FILE
    if has_default:
        return ConfigValueSource.DEFAULT
    return ConfigValueSource.DERIVED


def _sensitive_value_configured(
    raw_value: object,
    effective_value: object,
    *,
    file_present: bool,
    effective_present: bool,
    environment_resolved: bool,
) -> bool:
    if not file_present:
        return False
    if _contains_environment_placeholder(raw_value) and not environment_resolved:
        return False
    candidate = effective_value if effective_present else raw_value
    if candidate is None:
        return False
    if type(candidate) is str:
        return bool(candidate.strip())
    if type(candidate) in (list, tuple, dict):
        return bool(candidate)
    return True


def _apply_mode_summary(specs: list[ConfigFieldSpec]) -> str:
    modes: list[str] = []
    for spec in specs:
        mode = spec.apply_mode.value
        if mode not in modes:
            modes.append(mode)
    return ",".join(modes) or "none"


@contextlib.contextmanager
def _configuration_file_lock(path: Path) -> Iterator[None]:
    """使用与 Plugin Manager 相同的 ``<config>.lock`` 锁定约定。"""
    lock_path = path.with_name(f"{path.name}.lock")
    try:
        if lock_path.is_symlink():
            raise ConfigWriteFailed()
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o600)
    except ConfigManagementError:
        raise
    except OSError as exc:
        raise ConfigWriteFailed() from exc

    try:
        lock_file = os.fdopen(descriptor, "r+b")
    except (OSError, ValueError) as exc:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise ConfigWriteFailed() from exc
    acquired = False
    try:
        lock_identity = os.fstat(lock_file.fileno())
        if not stat.S_ISREG(lock_identity.st_mode):
            raise ConfigWriteFailed()
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            acquired = True
        except (ImportError, OSError) as exc:
            raise ConfigWriteFailed() from exc
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                # 释放失败不能覆盖主体错误，句柄关闭仍会释放系统锁。
                pass
        lock_file.close()


def _apply_descriptor_mode(descriptor: int, mode: int) -> None:
    try:
        os.fchmod(descriptor, mode)
    except AttributeError:
        # Windows 不提供 fchmod 时，关闭后会再通过路径保持权限。
        return
    except OSError as exc:
        raise ConfigWriteFailed() from exc


def _apply_file_mode(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError as exc:
        raise ConfigWriteFailed() from exc


def _fsync_parent_best_effort(parent: Path) -> None:
    if os.name == "nt":
        return
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(parent, flags)
        os.fsync(descriptor)
    except OSError:
        # 某些文件系统不支持目录 fsync；文件替换已原子完成。
        pass
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _remove_temporary_best_effort(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _log_operation_success(changed_count: int, apply_mode: str) -> None:
    logger.info(
        "Dashboard config repository operation completed: "
        "config_stage=commit changed_field_count=%d apply_mode=%s "
        "outcome=success exception_type=none",
        changed_count,
        apply_mode,
    )


def _log_operation_failure(
    stage: str,
    exc: Exception,
    *,
    changed_count: int = 0,
    apply_mode: str = "none",
) -> None:
    logger.warning(
        "Dashboard config repository operation failed: "
        "config_stage=%s changed_field_count=%d apply_mode=%s "
        "outcome=failed exception_type=%s",
        stage,
        changed_count,
        apply_mode,
        type(exc).__name__,
    )


__all__ = ["YamlConfigRepository"]
