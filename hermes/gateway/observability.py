"""Gateway 安全审计日志的稳定标识工具。"""

from __future__ import annotations

import hashlib
from typing import Any


def safe_identifier_digest(value: Any, *, label: str) -> str:
    """把路由、消息等外部标识转换为不可逆且可关联的短摘要。"""
    encoded = str(value or "").encode("utf-8", errors="surrogatepass")
    digest = hashlib.sha256(encoded).hexdigest()[:16]
    return f"{label}=sha256:{digest}"


def safe_route_digest(route_key: Any) -> str:
    """生成不暴露 chat/user 标识的 route 摘要。"""
    return safe_identifier_digest(route_key, label="route")


def safe_message_digest(message_id: Any) -> str:
    """生成不暴露平台消息 ID 的 Inbox 摘要。"""
    return safe_identifier_digest(message_id, label="inbox_message_id")
