"""
平台无关消息类型。

所有平台 adapter 把自己的原生事件翻译成 MessageEvent,下游
(GatewayRunner / conversation loop)只看这个结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


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
    attachments: list[dict] = field(default_factory=list)
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
