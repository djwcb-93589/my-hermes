"""
Platform-agnostic message types.

All platform adapters translate their native events into MessageEvent. Downstream
code (GatewayRunner, conversation loop) only sees this structure.
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
    """Where the message came from."""
    platform: str    # "console", "telegram", "wecom", ...
    chat_id: str     # unique chat identifier
    chat_type: str   # "dm" or "group"
    user_id: str     # who sent this message
    user_name: str = ""


@dataclass
class MessageEvent:
    """A platform-agnostic inbound message."""
    message_id: str
    text: str
    source: SessionSource
    message_type: MessageType = MessageType.TEXT
    media_urls: list[str] = field(default_factory=list)


def build_session_key(source: SessionSource, agent_name: str = "main") -> str:
    """
    Build the session key that uniquely identifies a conversation.

    格式: agent:{name}:{platform}:{chat_type}:{chat_id}[:user_id]
    群聊按 user_id 隔离 → 同一群里的张三和李四各自有独立对话。
    """
    parts = [f"agent:{agent_name}:{source.platform}:{source.chat_type}:{source.chat_id}"]
    if source.chat_type == "group":
        parts.append(source.user_id)
    return ":".join(parts)
