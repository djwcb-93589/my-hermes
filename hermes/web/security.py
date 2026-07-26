"""Web 控制接口的最小认证与 Origin 校验。"""

from __future__ import annotations

import hashlib
import secrets
from urllib.parse import urlparse


class ControlUnavailable(Exception):
    """控制能力未安全配置或无法使用。"""


class ControlForbidden(Exception):
    """控制请求未通过 Token 或本机 Origin 校验。"""


class ControlConflict(Exception):
    """控制请求与当前 Cron 运行状态冲突。"""


class ControlNotFound(Exception):
    """控制目标不存在或已经删除。"""


class ControlBadRequest(Exception):
    """控制请求缺少或包含无效的客户端幂等标识。"""


class ControlAuthenticator:
    """仅保存控制 Token 的摘要，不保存明文 Token。"""

    def __init__(self, token_digest: str | None = None):
        self._token_digest = token_digest

    @classmethod
    def from_token(cls, token: str | None) -> "ControlAuthenticator":
        if not isinstance(token, str) or len(token) < 32 or token != token.strip():
            return cls()
        return cls(hashlib.sha256(token.encode("utf-8")).hexdigest())

    def require(self, token: str | None, origin: str | None) -> None:
        if origin is not None and not _is_local_origin(origin):
            raise ControlForbidden()
        if self._token_digest is None:
            raise ControlUnavailable()
        if not isinstance(token, str):
            raise ControlForbidden()
        candidate = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if not secrets.compare_digest(candidate, self._token_digest):
            raise ControlForbidden()


def _is_local_origin(origin: str) -> bool:
    """只接受带可选端口的本机 HTTP(S) Origin。"""
    parsed = urlparse(origin)
    try:
        parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname in {"localhost", "127.0.0.1"}
        and not parsed.username
        and not parsed.password
        and not parsed.path
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
    )
