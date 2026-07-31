"""通用隔离 Agent Session 初始化的稳定错误边界。"""

from __future__ import annotations

import re


_MAX_ERROR_TYPE_LENGTH = 256
_MAX_SAFE_MESSAGE_LENGTH = 1_000
_SAFE_ERROR_TYPE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_FORBIDDEN_SAFE_MESSAGE_MARKERS = (
    "api key",
    "api_key",
    "authorization",
    "bearer ",
    "claim token",
    "claim_token",
    "password",
    "token=",
)


class IsolatedAgentSessionSetupError(Exception):
    """仅携带可公开摘要的隔离 Session 初始化失败。"""

    __slots__ = ("_error_type", "_retryable", "_safe_message")

    def __init__(
        self,
        safe_message: str,
        *,
        error_type: str,
        retryable: bool,
    ) -> None:
        if type(safe_message) is not str:
            raise TypeError("session setup safe_message must be a string")
        try:
            safe_message.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(
                "session setup safe_message must contain valid Unicode"
            ) from exc
        normalized_message = safe_message.lower()
        if (
            not safe_message.strip()
            or len(safe_message) > _MAX_SAFE_MESSAGE_LENGTH
            or any(ord(character) < 32 for character in safe_message)
            or any(character in safe_message for character in "\\/=")
            or any(
                marker in normalized_message
                for marker in _FORBIDDEN_SAFE_MESSAGE_MARKERS
            )
        ):
            raise ValueError("session setup safe_message is invalid")
        if (
            type(error_type) is not str
            or not error_type
            or len(error_type) > _MAX_ERROR_TYPE_LENGTH
            or _SAFE_ERROR_TYPE_RE.fullmatch(error_type) is None
        ):
            raise ValueError("session setup error_type is invalid")
        if type(retryable) is not bool:
            raise TypeError("session setup retryable must be a boolean")
        super().__init__(safe_message)
        self._safe_message = safe_message
        self._error_type = error_type
        self._retryable = retryable

    @property
    def safe_message(self) -> str:
        """返回已经过边界校验的单行安全摘要。"""

        return self._safe_message

    @property
    def error_type(self) -> str:
        """返回稳定、受限的错误标识。"""

        return self._error_type

    @property
    def retryable(self) -> bool:
        """返回调用方明确提供的可重试语义。"""

        return self._retryable


__all__ = ["IsolatedAgentSessionSetupError"]
