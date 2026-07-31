"""安全配置快照与显式修改路由。"""

from __future__ import annotations

import math

from fastapi import APIRouter, Request

from hermes.configuration import (
    ConfigFieldDescriptor,
    ConfigPatch,
    ConfigPatchChange,
    ConfigPatchResult,
    ConfigReadService,
    ConfigRevision,
    ConfigSnapshot,
    ConfigUnavailable,
    ConfigValue,
    ConfigValueInvalid,
    ConfigWriteService,
)
from hermes.web.schemas import (
    ConfigFieldResponse,
    ConfigPatchRequest,
    ConfigPatchResultResponse,
    ConfigSnapshotResponse,
    ConfigValueFieldResponse,
    SensitiveConfigFieldResponse,
)


router = APIRouter(prefix="/api/config", tags=["config"])


def _read_service(request: Request) -> ConfigReadService:
    """取得装配期注入的配置读取服务。"""
    service = getattr(request.app.state, "config_read_service", None)
    if service is None or not callable(getattr(service, "read", None)):
        raise ConfigUnavailable()
    return service


def _write_service(request: Request) -> ConfigWriteService:
    """取得装配期注入的配置写入服务。"""
    service = getattr(request.app.state, "config_write_service", None)
    if service is None or not callable(getattr(service, "apply", None)):
        raise ConfigUnavailable()
    return service


@router.get("", response_model=ConfigSnapshotResponse)
def get_config(request: Request) -> ConfigSnapshotResponse:
    """读取显式允许展示的配置字段，不返回原始配置文档。"""
    return _snapshot_response(_read_service(request).read())


@router.patch("", response_model=ConfigPatchResultResponse)
def patch_config(
    request: Request,
    body: ConfigPatchRequest,
) -> ConfigPatchResultResponse:
    """按公共字段名称提交修改，仅返回后续应用提示。"""
    patch = _patch_contract(body)
    return _patch_result_response(_write_service(request).apply(patch))


def _patch_contract(body: ConfigPatchRequest) -> ConfigPatch:
    """把隔离请求模型显式转换为中立修改契约。"""
    try:
        revision = ConfigRevision(body.expected_revision)
        changes = tuple(
            ConfigPatchChange(
                name=change.name,
                value=_patch_value(change.value),
            )
            for change in body.changes
        )
        return ConfigPatch(
            expected_revision=revision,
            changes=changes,
        )
    except (TypeError, ValueError) as exc:
        raise ConfigValueInvalid() from exc


def _patch_value(value: object) -> ConfigValue:
    """拒绝自由对象，并把请求列表冻结为中立契约使用的 tuple。"""
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is list and all(type(item) is str for item in value):
        return tuple(value)
    raise ConfigValueInvalid()


def _snapshot_response(snapshot: ConfigSnapshot) -> ConfigSnapshotResponse:
    """逐字段转换安全快照，不透传领域对象的未来扩展字段。"""
    if not isinstance(snapshot, ConfigSnapshot):
        raise ConfigUnavailable()
    try:
        return ConfigSnapshotResponse(
            revision=snapshot.revision.value,
            fields=[
                _field_response(descriptor)
                for descriptor in snapshot.fields
            ],
        )
    except ConfigUnavailable:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise ConfigUnavailable() from exc


def _field_response(
    descriptor: ConfigFieldDescriptor,
) -> ConfigFieldResponse:
    """严格区分普通字段和不含值信息的敏感字段。"""
    if not isinstance(descriptor, ConfigFieldDescriptor):
        raise ConfigUnavailable()
    if descriptor.sensitive:
        return SensitiveConfigFieldResponse(
            name=descriptor.name,
            configured=descriptor.configured,
            writable=False,
            sensitive=True,
        )
    if descriptor.source is None:
        raise ConfigUnavailable()
    return ConfigValueFieldResponse(
        name=descriptor.name,
        file_value=_response_value(descriptor.file_value),
        effective_value=_response_value(descriptor.effective_value),
        source=descriptor.source,
        value_type=descriptor.value_type,
        writable=descriptor.writable,
        sensitive=False,
        apply_mode=descriptor.apply_mode,
        nullable=descriptor.nullable,
        configured=descriptor.configured,
        shadowed_by_environment=descriptor.shadowed_by_environment,
        description=descriptor.description,
    )


def _patch_result_response(
    result: ConfigPatchResult,
) -> ConfigPatchResultResponse:
    """显式映射修改结果，不返回旧值或内部持久化状态。"""
    if not isinstance(result, ConfigPatchResult):
        raise ConfigUnavailable()
    try:
        return ConfigPatchResultResponse(
            previous_revision=result.previous_revision.value,
            new_revision=result.new_revision.value,
            changed_fields=list(result.changed_fields),
            apply_modes=list(result.apply_modes),
            restart_required=result.restart_required,
            restart_targets=list(result.restart_targets),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ConfigUnavailable() from exc


def _response_value(
    value: object,
) -> bool | int | float | str | list[str] | None:
    """仅转换中立契约允许的有限标量和字符串列表。"""
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is tuple and all(type(item) is str for item in value):
        return list(value)
    raise ConfigUnavailable()


__all__ = ["router"]
