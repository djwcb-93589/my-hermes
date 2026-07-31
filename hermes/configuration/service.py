"""只依赖中立 Repository Protocol 的配置读取和修改服务。"""

from __future__ import annotations

import logging

from .contracts import (
    MAX_CONFIG_PATCH_CHANGES,
    ConfigApplyMode,
    ConfigConflict,
    ConfigFieldDescriptor,
    ConfigFieldReadOnly,
    ConfigFieldSpec,
    ConfigFieldUnknown,
    ConfigManagementError,
    ConfigPatch,
    ConfigPatchChange,
    ConfigPatchResult,
    ConfigRepository,
    ConfigRepositorySnapshot,
    ConfigRepositoryWriteResult,
    ConfigShadowed,
    ConfigSnapshot,
    ConfigStoredField,
    ConfigUnavailable,
    ConfigValueInvalid,
    ConfigValueSource,
    normalize_config_value,
    safe_stored_config_value,
)
from .fields import ConfigFieldRegistry


logger = logging.getLogger(__name__)


class ConfigReadService:
    """把 Repository 字段状态投影为不泄漏敏感值的配置快照。"""

    __slots__ = ("_registry", "_repository")

    def __init__(
        self,
        repository: ConfigRepository,
        registry: ConfigFieldRegistry,
    ) -> None:
        _require_repository(repository)
        if not isinstance(registry, ConfigFieldRegistry):
            raise TypeError("registry must be a ConfigFieldRegistry")
        self._repository = repository
        self._registry = registry

    def read(self) -> ConfigSnapshot:
        """读取一次稳定修订，并按注册顺序构造安全字段。"""
        try:
            repository_snapshot = self._repository.read_snapshot()
            fields = _project_snapshot(repository_snapshot, self._registry)
            return ConfigSnapshot(
                revision=repository_snapshot.revision,
                fields=fields,
            )
        except ConfigManagementError as exc:
            _log_failure("read", exc)
            raise
        except Exception as exc:
            _log_failure("read", exc)
            raise ConfigUnavailable() from exc


class ConfigWriteService:
    """校验公共字段修改并汇总重启提示，不控制任何运行组件。"""

    __slots__ = ("_registry", "_repository")

    def __init__(
        self,
        repository: ConfigRepository,
        registry: ConfigFieldRegistry,
    ) -> None:
        _require_repository(repository)
        if not isinstance(registry, ConfigFieldRegistry):
            raise TypeError("registry must be a ConfigFieldRegistry")
        self._repository = repository
        self._registry = registry

    def apply(self, patch: ConfigPatch) -> ConfigPatchResult:
        """以 expected revision 执行一次有限、原子的配置修改。"""
        try:
            normalized_patch = self._validate_patch(patch)
            repository_result = self._repository.apply_patch(
                normalized_patch,
            )
            return self._result(repository_result, normalized_patch)
        except ConfigManagementError as exc:
            _log_failure("write", exc)
            raise
        except Exception as exc:
            _log_failure("write", exc)
            raise ConfigUnavailable() from exc

    def _validate_patch(self, patch: ConfigPatch) -> ConfigPatch:
        if not isinstance(patch, ConfigPatch):
            raise ConfigValueInvalid()
        if (
            type(patch.changes) is not tuple
            or not 1 <= len(patch.changes) <= MAX_CONFIG_PATCH_CHANGES
        ):
            raise ConfigValueInvalid()

        snapshot = self._repository.read_snapshot()
        stored_by_name = _stored_fields(snapshot, self._registry)
        if snapshot.revision != patch.expected_revision:
            raise ConfigConflict()

        seen: set[str] = set()
        normalized: list[ConfigPatchChange] = []
        for change in patch.changes:
            if not isinstance(change, ConfigPatchChange):
                raise ConfigValueInvalid()
            if type(change.name) is not str or change.name in seen:
                raise ConfigValueInvalid()
            seen.add(change.name)
            spec = self._registry.get(change.name)
            if spec is None:
                raise ConfigFieldUnknown()
            if not spec.writable or spec.sensitive:
                raise ConfigFieldReadOnly()
            if _is_environment_shadowed(
                stored_by_name[spec.public_name]
            ):
                raise ConfigShadowed()
            try:
                value = normalize_config_value(spec, change.value)
            except (TypeError, ValueError) as exc:
                raise ConfigValueInvalid() from exc
            normalized.append(
                ConfigPatchChange(name=spec.public_name, value=value)
            )
        return ConfigPatch(
            expected_revision=patch.expected_revision,
            changes=tuple(normalized),
        )

    def _result(
        self,
        result: ConfigRepositoryWriteResult,
        patch: ConfigPatch,
    ) -> ConfigPatchResult:
        if not isinstance(result, ConfigRepositoryWriteResult):
            raise ConfigUnavailable()
        if result.previous_revision != patch.expected_revision:
            raise ConfigUnavailable()
        requested_names = {change.name for change in patch.changes}
        if (
            type(result.changed_fields) is not tuple
            or len(set(result.changed_fields)) != len(result.changed_fields)
            or any(name not in requested_names for name in result.changed_fields)
        ):
            raise ConfigUnavailable()

        modes: list[ConfigApplyMode] = []
        for name in result.changed_fields:
            spec = self._registry.get(name)
            if spec is None:
                raise ConfigUnavailable()
            if spec.apply_mode not in modes:
                modes.append(spec.apply_mode)
        targets = _restart_targets(tuple(modes))
        return ConfigPatchResult(
            previous_revision=result.previous_revision,
            new_revision=result.new_revision,
            changed_fields=result.changed_fields,
            apply_modes=tuple(modes),
            restart_required=bool(targets),
            restart_targets=targets,
        )


def _require_repository(repository: object) -> None:
    if not callable(getattr(repository, "read_snapshot", None)) or not callable(
        getattr(repository, "apply_patch", None)
    ):
        raise TypeError("repository must implement ConfigRepository")


def _stored_fields(
    snapshot: ConfigRepositorySnapshot,
    registry: ConfigFieldRegistry,
) -> dict[str, ConfigStoredField]:
    """校验 Repository 没有遗漏、重复或额外投影字段。"""
    if not isinstance(snapshot, ConfigRepositorySnapshot):
        raise ConfigUnavailable()
    by_name: dict[str, ConfigStoredField] = {}
    for field in snapshot.fields:
        if not isinstance(field, ConfigStoredField):
            raise ConfigUnavailable()
        if field.name in by_name or registry.get(field.name) is None:
            raise ConfigUnavailable()
        by_name[field.name] = field
    expected = {spec.public_name for spec in registry.fields}
    if set(by_name) != expected:
        raise ConfigUnavailable()
    return by_name


def _project_snapshot(
    snapshot: ConfigRepositorySnapshot,
    registry: ConfigFieldRegistry,
) -> tuple[ConfigFieldDescriptor, ...]:
    stored_by_name = _stored_fields(snapshot, registry)
    projected: list[ConfigFieldDescriptor] = []
    for spec in registry.fields:
        stored = stored_by_name[spec.public_name]
        if spec.sensitive:
            if (
                stored.file_value is not None
                or stored.effective_value is not None
            ):
                raise ConfigUnavailable()
            projected.append(
                ConfigFieldDescriptor(
                    name=spec.public_name,
                    value_type=spec.value_type,
                    writable=False,
                    sensitive=True,
                    apply_mode=spec.apply_mode,
                    nullable=spec.nullable,
                    configured=stored.configured,
                    description=spec.description,
                )
            )
            continue

        file_value = _validated_file_value(stored.file_value)
        environment_shadowed = _is_environment_shadowed(stored)
        effective_value = _validated_effective_value(
            spec,
            stored,
        )
        projected.append(
            ConfigFieldDescriptor(
                name=spec.public_name,
                value_type=spec.value_type,
                writable=spec.writable,
                sensitive=False,
                apply_mode=spec.apply_mode,
                nullable=spec.nullable,
                configured=stored.configured,
                source=stored.source,
                file_value=file_value,
                effective_value=effective_value,
                description=spec.description,
                shadowed_by_environment=environment_shadowed,
            )
        )
    return tuple(projected)


def _validated_file_value(
    value: object,
):
    """只执行有界安全复制，保留 YAML 中存储值的原始标量语义。"""
    try:
        return safe_stored_config_value(value)
    except (TypeError, ValueError) as exc:
        raise ConfigUnavailable() from exc


def _validated_effective_value(
    spec: ConfigFieldSpec,
    stored: ConfigStoredField,
):
    """环境来源刻意隐藏值；其他来源必须满足正式字段声明。"""
    if _is_environment_shadowed(stored):
        if (
            stored.source is not ConfigValueSource.ENVIRONMENT
            or stored.effective_value is not None
        ):
            raise ConfigUnavailable()
        return None
    try:
        return normalize_config_value(spec, stored.effective_value)
    except (TypeError, ValueError) as exc:
        raise ConfigUnavailable() from exc


def _is_environment_shadowed(stored: ConfigStoredField) -> bool:
    """兼容旧 Repository 的 environment 来源，并统一为显式覆盖标记。"""
    return (
        stored.shadowed_by_environment
        or stored.source is ConfigValueSource.ENVIRONMENT
    )


def _restart_targets(
    modes: tuple[ConfigApplyMode, ...],
) -> tuple[str, ...]:
    targets: list[str] = []
    mapping = {
        ConfigApplyMode.GATEWAY_RESTART: "gateway",
        ConfigApplyMode.DASHBOARD_RESTART: "dashboard",
        ConfigApplyMode.APPLICATION_RESTART: "application",
    }
    for mode in modes:
        target = mapping.get(mode)
        if target is not None and target not in targets:
            targets.append(target)
    return tuple(targets)


def _log_failure(stage: str, exc: Exception) -> None:
    """日志只记录稳定阶段和异常类型，不记录字段、值或修订号。"""
    logger.warning(
        "Dashboard config operation failed: "
        "config_stage=%s outcome=failed exception_type=%s",
        stage,
        type(exc).__name__,
    )


__all__ = ["ConfigReadService", "ConfigWriteService"]
