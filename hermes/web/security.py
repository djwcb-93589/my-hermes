"""Dashboard 的 Token 认证、绑定边界与 Origin 校验。"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse


TOKEN_HEADER = "X-Hermes-Control-Token"


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
    """仅保存 Dashboard Token 的摘要，不保存明文 Token。"""

    def __init__(self, token_digest: str | None = None):
        self._token_digest = token_digest

    @classmethod
    def from_token(cls, token: str | None) -> "ControlAuthenticator":
        return cls(cls.digest_token(token))

    @classmethod
    def from_digest(cls, token_digest: str | None) -> "ControlAuthenticator":
        """从已校验摘要创建认证器，避免启动配置长期持有明文 Token。"""
        if not cls.is_valid_digest(token_digest):
            raise ValueError("dashboard control token digest is invalid")
        return cls(token_digest)

    @staticmethod
    def digest_token(token: object) -> str | None:
        """验证 Token 基本形状并返回不可逆摘要。"""
        if not isinstance(token, str) or len(token) < 32 or token != token.strip():
            return None
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def is_valid_digest(token_digest: object) -> bool:
        """校验启动装配传入的是 SHA-256 摘要或空值。"""
        if token_digest is None:
            return True
        if not isinstance(token_digest, str) or len(token_digest) != 64:
            return False
        return all(character in "0123456789abcdef" for character in token_digest)

    @property
    def is_configured(self) -> bool:
        """标记当前实例是否具备可验证的 Token 摘要。"""
        return self._token_digest is not None

    def verifies(self, token: str | None) -> bool:
        """以常量时间比较 Token 摘要，不暴露 Token 配置细节。"""
        if self._token_digest is None or not isinstance(token, str):
            return False
        candidate = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return secrets.compare_digest(candidate, self._token_digest)

    def require(self, token: str | None, origin: str | None) -> None:
        """保留旧控制调用方的最小认证接口。"""
        if origin is not None and not _is_local_origin(origin):
            raise ControlForbidden()
        if self._token_digest is None:
            raise ControlUnavailable()
        if not self.verifies(token):
            raise ControlForbidden()


class DashboardPermission(str, Enum):
    """Dashboard 当前支持的两类权限边界。"""

    READ = "dashboard:read"
    CONTROL = "dashboard:control"


@dataclass(frozen=True)
class AccessDecision:
    """应用层认证中间件使用的无敏感信息授权结果。"""

    allowed: bool
    status_code: int | None = None
    error_code: str | None = None


class DashboardAccessPolicy:
    """集中判断读取与控制权限，不把认证规则散落到路由函数。"""

    def __init__(
        self,
        authenticator: ControlAuthenticator,
        *,
        read_auth_required: bool,
        bound_host: str,
    ) -> None:
        self._authenticator = authenticator
        if not isinstance(read_auth_required, bool):
            raise ValueError("dashboard read_auth_required must be a boolean")
        self._read_auth_required = read_auth_required
        self._bound_host = str(bound_host)

    @property
    def bound_host(self) -> str:
        """返回已校验的绑定主机，仅供 CORS 规则生成使用。"""
        return self._bound_host

    def authorize(
        self,
        permission: DashboardPermission,
        *,
        token: str | None,
        origin: str | None,
    ) -> AccessDecision:
        """返回统一授权结果；未认证不区分 Token 缺失和错误。"""
        if permission is DashboardPermission.READ:
            if not self._read_auth_required:
                return AccessDecision(allowed=True)
            return self._token_decision(token)

        if permission is DashboardPermission.CONTROL:
            token_decision = self._token_decision(token)
            if not token_decision.allowed:
                return token_decision
            if origin is not None and not is_allowed_dashboard_origin(
                origin,
                self._bound_host,
            ):
                return AccessDecision(
                    allowed=False,
                    status_code=403,
                    error_code="control_origin_forbidden",
                )
            return AccessDecision(allowed=True)

        return AccessDecision(
            allowed=False,
            status_code=403,
            error_code="permission_forbidden",
        )

    def _token_decision(
        self,
        token: str | None,
    ) -> AccessDecision:
        """统一隐藏 Token 是缺失、错误还是未配置的细节。"""
        if not self._authenticator.is_configured:
            return AccessDecision(
                allowed=False,
                status_code=401,
                error_code="authentication_required",
            )
        if not self._authenticator.verifies(token):
            return AccessDecision(
                allowed=False,
                status_code=401,
                error_code="authentication_required",
            )
        return AccessDecision(allowed=True)


def is_loopback_host(host: object) -> bool:
    """只把明确的回环地址或名称视为无需强制读取认证的绑定。"""
    if not isinstance(host, str):
        return False
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        # 不做 DNS 解析；未知主机名按非回环地址 fail-closed。
        return False


def cors_origin_regex(bound_host: str) -> str:
    """只允许与安全绑定规则相符的浏览器 Origin。"""
    normalized = _normalized_host(bound_host)
    if is_loopback_host(normalized):
        hosts = r"localhost|127\.0\.0\.1|\[::1\]"
    elif normalized and normalized not in {"0.0.0.0", "::"}:
        hosts = (
            rf"\[{re.escape(normalized)}\]"
            if ":" in normalized
            else re.escape(normalized)
        )
    else:
        return r"(?!)"
    return rf"^https?://(?:{hosts})(?::\d+)?$"


def is_allowed_dashboard_origin(origin: str, bound_host: str) -> bool:
    """控制请求仅接受本机或明确配置的 Dashboard Origin。"""
    parsed = _parse_http_origin(origin)
    if parsed is None:
        return False
    origin_host = _normalized_host(parsed.hostname)
    if origin_host is None:
        return False
    bound = _normalized_host(bound_host)
    if bound in {None, "0.0.0.0", "::"}:
        return False
    if is_loopback_host(bound):
        return is_loopback_host(origin_host)
    return origin_host == bound


def _is_local_origin(origin: str) -> bool:
    """只接受带可选端口的本机 HTTP(S) Origin。"""
    parsed = _parse_http_origin(origin)
    return parsed is not None and is_loopback_host(parsed.hostname)


def _parse_http_origin(origin: str):
    """解析不含路径、用户信息和查询参数的 HTTP(S) Origin。"""
    parsed = urlparse(origin)
    try:
        parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return None
    return parsed


def _normalized_host(host: object) -> str | None:
    """统一比较 hostname，IPv6 以不带方括号的形式保存。"""
    if not isinstance(host, str) or not host:
        return None
    normalized = host.strip().lower()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    return normalized or None
