"""
平台无关消息类型。

所有平台 adapter 把自己的原生事件翻译成 MessageEvent,下游
(GatewayRunner / conversation loop)只看这个结构。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Mapping, TypedDict, cast


AttachmentSourceType = Literal["image", "file", "audio", "media"]
AttachmentResourceType = Literal["image", "file"]
AttachmentStatus = Literal["pending", "ready", "failed"]


class Attachment(TypedDict):
    """可直接写入 Queue JSON 的平台无关附件描述。"""

    source_type: AttachmentSourceType
    resource_key: str
    resource_type: AttachmentResourceType
    original_name: str | None
    local_path: str | None
    mime_type: str | None
    size_bytes: int | None
    sha256: str | None
    status: AttachmentStatus
    error_code: str | None


# 允许平台以后增加自己的扩展字段，同时集中约束跨平台公共字段。
_ATTACHMENT_SOURCE_TYPES = frozenset({"image", "file", "audio", "media"})
_ATTACHMENT_RESOURCE_TYPES = frozenset({"image", "file"})
_ATTACHMENT_STATUSES = frozenset({"pending", "ready", "failed"})
_ATTACHMENT_NULLABLE_STRING_FIELDS = (
    "original_name",
    "local_path",
    "mime_type",
    "sha256",
    "error_code",
)


def validate_attachment(value: object) -> Attachment:
    """校验并补齐一个附件，不把它转换成非 JSON 对象。"""
    if not isinstance(value, Mapping):
        raise ValueError("attachment must be a mapping")
    if not all(isinstance(key, str) for key in value):
        raise ValueError("attachment keys must be strings")

    attachment = dict(value)
    source_type = attachment.get("source_type")
    if (
        not isinstance(source_type, str)
        or source_type not in _ATTACHMENT_SOURCE_TYPES
    ):
        raise ValueError(
            "attachment.source_type must be one of: "
            "image, file, audio, media"
        )

    resource_key = attachment.get("resource_key")
    if not isinstance(resource_key, str) or not resource_key.strip():
        raise ValueError("attachment.resource_key must be a non-empty string")

    resource_type = attachment.get("resource_type")
    if (
        not isinstance(resource_type, str)
        or resource_type not in _ATTACHMENT_RESOURCE_TYPES
    ):
        raise ValueError(
            "attachment.resource_type must be one of: image, file"
        )

    status = attachment.get("status")
    if not isinstance(status, str) or status not in _ATTACHMENT_STATUSES:
        raise ValueError(
            "attachment.status must be one of: pending, ready, failed"
        )

    for field_name in _ATTACHMENT_NULLABLE_STRING_FIELDS:
        field_value = attachment.setdefault(field_name, None)
        if field_value is not None and not isinstance(field_value, str):
            raise ValueError(
                f"attachment.{field_name} must be a string or null"
            )

    size_bytes = attachment.setdefault("size_bytes", None)
    if (
        size_bytes is not None
        and (
            isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
        )
    ):
        raise ValueError(
            "attachment.size_bytes must be a non-negative integer or null"
        )

    try:
        json.dumps(attachment, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "attachment values must be JSON-compatible"
        ) from exc

    # 复制后的普通 dict 保持旧 Queue / Outbox 可直接 JSON 序列化的语义。
    return cast(Attachment, attachment)


def validate_attachments(values: object) -> list[Attachment]:
    """集中校验附件列表，并返回不共享调用方容器的新列表。"""
    if not isinstance(values, list):
        raise ValueError("attachments must be a list")
    return [validate_attachment(value) for value in values]


class MessageType(Enum):
    TEXT = "text"
    PHOTO = "photo"
    VOICE = "voice"
    DOCUMENT = "document"


@dataclass
class SessionSource:
    """消息来源标识。

    - ``route_key`` 由 ``build_session_key`` 生成,用于 Gateway 内存路由。
    - ``conversation_id`` 是 DB session_id,可能因 /new 切换。
    """
    platform: str            # "cli" / "feishu" / "weixin" / ...
    account_id: str = ""     # 平台侧 bot / app 标识(如飞书 app_id)
    chat_id: str = ""        # 平台侧会话标识(群 ID / DM peer)
    chat_type: str = "dm"    # "dm" / "group" / "topic"
    user_id: str = ""        # 发送者主标识
    user_id_alt: str = ""    # 备用标识(如飞书 union_id)
    user_name: str = ""
    thread_id: str | None = None  # 话题 / 线程 ID


@dataclass
class MessageEvent:
    """统一入站消息。"""
    message_id: str
    text: str
    source: SessionSource
    message_type: MessageType = MessageType.TEXT
    media_urls: list[str] = field(default_factory=list)
    reply_to_message_id: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class SendResult:
    """统一出站结果。

    ``message_id`` 保留单消息调用的旧接口;分片发送时通过
    ``message_ids`` / ``sent_chunks`` 返回完整进度,让 Runner 能决定
    是否完成入站消息或从失败分片继续恢复。
    """
    success: bool
    message_id: str | None = None
    error: str | None = None
    error_code: str | None = None
    retryable: bool = False
    # Adapter 不应自行长时间 sleep；平台建议的等待由 Runner 限幅后写入
    # Outbox next_attempt_at，因而可以跨 shutdown 恢复。
    retry_after_seconds: float | None = None
    sent_chunks: int = 0
    total_chunks: int = 0
    failed_chunk_index: int | None = None
    message_ids: list[str] = field(default_factory=list)


def build_session_key(source: SessionSource, agent_name: str = "main") -> str:
    """构建稳定路由 key。

    格式:
      agent:{name}:{platform}:{account_id}:{chat_type}:{chat_id}[:user_id][:thread_id]

    - account_id 区分同一平台不同 bot 实例
    - 群聊 / 话题按 user_id 隔离 —— 同一群里不同用户各自独立对话
    - thread_id 额外区分话题子线程
    """
    parts = [
        f"agent:{agent_name}:{source.platform}:{source.account_id}"
        f":{source.chat_type}:{source.chat_id}"
    ]
    if source.chat_type in ("group", "topic"):
        parts.append(source.user_id)
    if source.thread_id:
        parts.append(source.thread_id)
    return ":".join(parts)
