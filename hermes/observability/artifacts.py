"""文件类结果的中立 Artifact 契约，不负责存储或下载。"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from hermes.observability.contracts import (
    _optional_error_type,
    freeze_safe_metadata,
)


# 常用 kind 的参考集合；ArtifactRecord.kind 仍允许受校验的新字符串。
COMMON_ARTIFACT_KINDS = frozenset({
    "file",
    "document",
    "pdf",
    "image",
    "screenshot",
    "browser_download",
    "archive",
    "other",
})
_ABSOLUTE_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/])")


class ArtifactStatus(str, Enum):
    """Artifact 生命周期状态；kind 保持开放字符串以兼容后续模块。"""

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _public_text(value: object, field_name: str) -> str:
    """拒绝会直接把绝对路径带入公开 Artifact 摘要的文本字段。"""
    text = _required_text(value, field_name)
    if _ABSOLUTE_PATH_RE.match(text):
        raise ValueError(f"{field_name} must not be an absolute path")
    return text


def _optional_public_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _public_text(value, field_name)


def _timestamp(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite number")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{field_name} must be a finite number")
    return normalized


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """内部 Artifact 记录；storage_ref 仅为未来存储层使用。"""

    artifact_id: str
    kind: str
    status: ArtifactStatus
    producer: str
    display_name: str
    media_type: str | None
    size_bytes: int | None
    storage_ref: str | None
    session_id: str | None
    run_id: str | None
    tool_call_id: str | None
    cron_run_id: str | None
    created_at: float
    updated_at: float
    error_type: str | None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """冻结 metadata 并阻止文件字节或异常对象；storage_ref 仅保留为内部引用。"""
        for field_name in ("artifact_id", "kind", "producer", "display_name"):
            object.__setattr__(
                self,
                field_name,
                _public_text(getattr(self, field_name), field_name),
            )
        if not isinstance(self.status, ArtifactStatus):
            raise TypeError("status must be an ArtifactStatus")
        for field_name in (
            "media_type",
            "session_id",
            "run_id",
            "tool_call_id",
            "cron_run_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_public_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "error_type",
            _optional_error_type(self.error_type, "error_type"),
        )
        object.__setattr__(
            self,
            "storage_ref",
            _optional_text(self.storage_ref, "storage_ref"),
        )
        if self.size_bytes is not None and (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise ValueError("size_bytes must be a non-negative integer or None")
        object.__setattr__(
            self,
            "created_at",
            _timestamp(self.created_at, "created_at"),
        )
        object.__setattr__(
            self,
            "updated_at",
            _timestamp(self.updated_at, "updated_at"),
        )
        object.__setattr__(
            self,
            "metadata",
            freeze_safe_metadata(self.metadata),
        )


class ArtifactPublisher(Protocol):
    """Artifact 记录的同步发布端。"""

    def publish(self, artifact: ArtifactRecord) -> None:
        """接收不含文件字节的 Artifact 记录。"""


class NullArtifactPublisher:
    """不持久化、不上传且不创建任务的空 Artifact 发布端。"""

    __slots__ = ()

    def publish(self, artifact: ArtifactRecord) -> None:
        del artifact


@dataclass(frozen=True, slots=True)
class ArtifactSummary:
    """可公开使用的 Artifact 投影，故意不包含 storage_ref。"""

    artifact_id: str
    kind: str
    status: str
    producer: str
    display_name: str
    media_type: str | None
    size_bytes: int | None
    session_id: str | None
    run_id: str | None
    tool_call_id: str | None
    cron_run_id: str | None
    created_at: float
    updated_at: float
    error_type: str | None
    has_storage_ref: bool


def project_artifact(artifact: ArtifactRecord) -> ArtifactSummary:
    """把内部 Artifact 记录转换为不包含内部存储定位信息的摘要。"""
    if not isinstance(artifact, ArtifactRecord):
        raise TypeError("artifact must be an ArtifactRecord")
    return ArtifactSummary(
        artifact_id=artifact.artifact_id,
        kind=artifact.kind,
        status=artifact.status.value,
        producer=artifact.producer,
        display_name=artifact.display_name,
        media_type=artifact.media_type,
        size_bytes=artifact.size_bytes,
        session_id=artifact.session_id,
        run_id=artifact.run_id,
        tool_call_id=artifact.tool_call_id,
        cron_run_id=artifact.cron_run_id,
        created_at=artifact.created_at,
        updated_at=artifact.updated_at,
        error_type=artifact.error_type,
        has_storage_ref=artifact.storage_ref is not None,
    )
