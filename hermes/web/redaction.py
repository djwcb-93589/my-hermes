"""Dashboard 对外输出的脱敏与截断组件。"""

from __future__ import annotations

import math
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from hermes.config_values import PROJECT_ROOT, hermes_home
from hermes.redaction import redact_explicit_secrets


REDACTED_VALUE = "<redacted>"
TRUNCATED_VALUE = "<truncated>"


@dataclass(frozen=True)
class DashboardOutputLimits:
    """Dashboard 所有输出共用的大小与结构边界。"""

    preview_text_limit: int = 300
    message_text_limit: int = 8_000
    structured_value_limit: int = 32_000
    error_text_limit: int = 2_000
    max_depth: int = 8
    max_list_items: int = 100
    max_dict_items: int = 100


_SENSITIVE_FIELD_NAMES = frozenset({
    "apikey",
    "accesstoken",
    "refreshtoken",
    "token",
    "secret",
    "clientsecret",
    "password",
    "passwd",
    "authorization",
    "proxyauthorization",
    "cookie",
    "setcookie",
    "privatekey",
    "credential",
    "credentials",
    "webhooksecret",
    "encryptkey",
    "encryptionkey",
    "verificationtoken",
    "xapikey",
})
_SENSITIVE_FIELD_SUFFIXES = frozenset({
    "apikey",
    "accesstoken",
    "refreshtoken",
    "token",
    "secret",
    "password",
    "passwd",
    "privatekey",
    "credential",
    "credentials",
})
_HEADER_RE = re.compile(
    r"(?im)(?P<prefix>(?<![A-Za-z0-9_-])(?:authorization|proxy-authorization|"
    r"cookie|set-cookie|x-api-key)\s*:\s*)(?P<value>[^\r\n]*)"
)
_ENV_ASSIGNMENT_RE = re.compile(
    r"(?im)(?P<prefix>(?<![A-Za-z0-9_])(?P<name>[A-Za-z_][A-Za-z0-9_-]*)"
    r"\s*=\s*)(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s\r\n]+)"
)
_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_WINDOWS_HOME_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:[\\/](?:users|documents and settings)[\\/]"
    r"[^\\/\s]+)"
)
_UNIX_HOME_RE = re.compile(r"(?<![A-Za-z0-9])/(?:home|Users)/[^/\s]+")
_UNIX_TEMP_RE = re.compile(r"(?<![A-Za-z0-9])/(?:tmp|var/tmp)(?=$|[/\\])")
_UNC_PATH_RE = re.compile(r"(?<![A-Za-z0-9])(?:\\\\|//)[^\\/\s]+[\\/][^\s<>'\"]+")
_WINDOWS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s<>'\"]*"
)
_WINDOWS_ROOTED_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9\\:])\\(?!\\)[^\s<>'\"]+"
)
_UNIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9])/(?!/)[^\s<>'\"]+")


class DashboardRedactor:
    """只转换 Dashboard 返回副本，不修改持久化或业务原始数据。"""

    def __init__(self, limits: DashboardOutputLimits | None = None):
        self.limits = limits or DashboardOutputLimits()
        self._known_roots = _known_roots()

    def preview_text(self, value: object) -> str:
        """脱敏后生成受限长度的预览文本。"""
        return self.redact_text(value, limit=self.limits.preview_text_limit)

    def message_text(self, value: object) -> str:
        """脱敏后生成受限长度的消息文本。"""
        return self.redact_text(value, limit=self.limits.message_text_limit)

    def error_text(self, value: object) -> str:
        """脱敏后生成受限长度的错误或结果摘要。"""
        return self.redact_text(value, limit=self.limits.error_text_limit)

    def redact_text(self, value: object, *, limit: int | None = None) -> str:
        """先脱敏字符串，再按字符边界安全截断。"""
        text = _coerce_text(value)
        redacted = redact_explicit_secrets(text).replace("<secret>", REDACTED_VALUE)
        redacted = _HEADER_RE.sub(_redact_header, redacted)
        redacted = _ENV_ASSIGNMENT_RE.sub(_redact_environment_assignment, redacted)
        redacted = self._redact_urls_and_paths(redacted)
        return _truncate_text(
            redacted,
            self.limits.structured_value_limit if limit is None else limit,
        )

    def redact_value(
        self,
        value: object,
        *,
        text_limit: int | None = None,
    ) -> object:
        """递归处理 JSON 兼容结构，并限制深度、项数和文本总量。"""
        budget = _StructuredBudget(self.limits.structured_value_limit)
        return self._redact_value(
            value,
            depth=0,
            budget=budget,
            text_limit=(
                self.limits.structured_value_limit
                if text_limit is None
                else text_limit
            ),
        )

    def _redact_value(
        self,
        value: object,
        *,
        depth: int,
        budget: "_StructuredBudget",
        text_limit: int,
    ) -> object:
        if depth >= self.limits.max_depth:
            return TRUNCATED_VALUE
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else TRUNCATED_VALUE
        if isinstance(value, str):
            return budget.take(self.redact_text(value, limit=text_limit))
        if isinstance(value, dict):
            result: dict[str, object] = {}
            item_limit = max(1, self.limits.max_dict_items)
            visible_limit = (
                item_limit
                if len(value) <= item_limit
                else item_limit - 1
            )
            for index, (key, item) in enumerate(value.items()):
                if index >= visible_limit:
                    break
                raw_key = _coerce_text(key)
                safe_key = budget.take(self.redact_text(raw_key, limit=text_limit))
                if _is_sensitive_field_name(raw_key):
                    result[safe_key] = REDACTED_VALUE
                else:
                    result[safe_key] = self._redact_value(
                        item,
                        depth=depth + 1,
                        budget=budget,
                        text_limit=text_limit,
                    )
            if len(value) > item_limit:
                result[TRUNCATED_VALUE] = TRUNCATED_VALUE
            return result
        if isinstance(value, (list, tuple)):
            result: list[object] = []
            item_limit = max(1, self.limits.max_list_items)
            visible_limit = (
                item_limit
                if len(value) <= item_limit
                else item_limit - 1
            )
            for item in value[:visible_limit]:
                result.append(self._redact_value(
                    item,
                    depth=depth + 1,
                    budget=budget,
                    text_limit=text_limit,
                ))
            if len(value) > item_limit:
                result.append(TRUNCATED_VALUE)
            return result
        return REDACTED_VALUE

    def _redact_urls_and_paths(self, text: str) -> str:
        """避免路径规则改写 URL path，同时处理普通文本中的绝对路径。"""
        parts: list[str] = []
        cursor = 0
        for match in _URL_RE.finditer(text):
            parts.append(self._redact_paths(text[cursor:match.start()]))
            parts.append(_redact_url(match.group(0)))
            cursor = match.end()
        parts.append(self._redact_paths(text[cursor:]))
        return "".join(parts)

    def _redact_paths(self, text: str) -> str:
        """优先替换已知根目录，再掩盖其他跨平台绝对路径。"""
        redacted = text
        root_markers: list[tuple[str, str]] = []
        for index, (root, placeholder) in enumerate(self._known_roots):
            marker = f"\x00dashboard-root-{index}x"
            for variant in _path_variants(root):
                pattern = re.compile(
                    re.escape(variant) + r"(?=$|[\\/])",
                    re.IGNORECASE if _is_windows_style(variant) else 0,
                )
                redacted = pattern.sub(marker, redacted)
            root_markers.append((marker, placeholder))
        redacted = _WINDOWS_ROOTED_PATH_RE.sub(_mask_absolute_path, redacted)
        home_marker = "\x00dashboard-home-rootx"
        temp_marker = "\x00dashboard-temp-rootx"
        root_markers.extend(((home_marker, "<HOME>"), (temp_marker, "<TEMP>")))
        redacted = _WINDOWS_HOME_RE.sub(home_marker, redacted)
        redacted = _UNIX_HOME_RE.sub(home_marker, redacted)
        redacted = _UNIX_TEMP_RE.sub(temp_marker, redacted)
        redacted = _UNC_PATH_RE.sub(_mask_unc_path, redacted)
        redacted = _WINDOWS_PATH_RE.sub(_mask_absolute_path, redacted)
        redacted = _UNIX_PATH_RE.sub(_mask_absolute_path, redacted)
        for marker, placeholder in root_markers:
            redacted = redacted.replace(marker, placeholder)
        return redacted


@dataclass
class _StructuredBudget:
    """防止单个结构化字段在递归后突破统一输出上限。"""

    remaining: int

    def take(self, value: str) -> str:
        if self.remaining <= 0:
            return TRUNCATED_VALUE
        if len(value) <= self.remaining:
            self.remaining -= len(value)
            return value
        result = _truncate_text(value, self.remaining)
        self.remaining = 0
        return result


def _coerce_text(value: object) -> str:
    """只接受基础文本转换，未知对象不调用其自定义表示。"""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, (bool, int, float)):
        return str(value)
    return REDACTED_VALUE


def _is_sensitive_field_name(value: object) -> bool:
    """按字段名识别凭证，而不因普通 id、session 或 key 误伤。"""
    if not isinstance(value, str):
        return False
    normalized = re.sub(r"[^a-z0-9]", "", value.lower())
    if normalized in _SENSITIVE_FIELD_NAMES:
        return True
    if normalized.startswith(("authorization", "proxyauthorization", "cookie")):
        return True
    return any(normalized.endswith(suffix) for suffix in _SENSITIVE_FIELD_SUFFIXES)


def _redact_header(match: re.Match[str]) -> str:
    return f"{match.group('prefix')}{REDACTED_VALUE}"


def _redact_environment_assignment(match: re.Match[str]) -> str:
    if not _is_sensitive_field_name(match.group("name")):
        return match.group(0)
    return f"{match.group('prefix')}{REDACTED_VALUE}"


def _redact_url(raw: str) -> str:
    """保留 URL 的可定位部分，并移除 userinfo、query value 与 fragment。"""
    try:
        parsed = urlsplit(raw)
        if not parsed.scheme or not parsed.netloc or not parsed.hostname:
            return _fallback_redact_url(raw)
        hostname = parsed.hostname
        host = f"[{hostname}]" if ":" in hostname else hostname
        try:
            port = parsed.port
        except ValueError:
            return _fallback_redact_url(raw)
        netloc = f"{host}:{port}" if port is not None else host
        query = _redact_query(parsed.query)
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))
    except (TypeError, ValueError):
        return _fallback_redact_url(raw)


def _fallback_redact_url(raw: str) -> str:
    """URL 解析失败时仍不返回 userinfo、query value 或 fragment。"""
    without_fragment = raw.split("#", 1)[0]
    prefix, separator, rest = without_fragment.partition("://")
    if not separator:
        return REDACTED_VALUE
    authority, slash, suffix = rest.partition("/")
    authority = authority.rsplit("@", 1)[-1]
    path_and_query = f"/{suffix}" if slash else ""
    path, question, query = path_and_query.partition("?")
    safe_query = _redact_query(query) if question else ""
    return f"{prefix}://{authority}{path}{'?' if question else ''}{safe_query}"


def _redact_query(query: str) -> str:
    """保留 query 参数名和顺序，但统一隐藏所有值。"""
    if not query:
        return ""
    values: list[str] = []
    for part in query.split("&"):
        key = part.split("=", 1)[0]
        values.append(f"{key}={REDACTED_VALUE}")
    return "&".join(values)


def _mask_unc_path(match: re.Match[str]) -> str:
    return _masked_path(match.group(0), "<UNC_PATH>")


def _mask_absolute_path(match: re.Match[str]) -> str:
    return _masked_path(match.group(0), "<ABS_PATH>")


def _masked_path(raw: str, placeholder: str) -> str:
    """保留文件名和分隔符风格，不公开父目录。"""
    trailing = ""
    candidate = raw
    while candidate and candidate[-1] in ".,;:)":
        trailing = candidate[-1] + trailing
        candidate = candidate[:-1]
    separator = "\\" if "\\" in candidate and "/" not in candidate else "/"
    components = [item for item in re.split(r"[\\/]+", candidate) if item]
    filename = components[-1] if components else ""
    if not filename or filename.endswith(":"):
        return placeholder + trailing
    return f"{placeholder}{separator}{filename}{trailing}"


def _truncate_text(value: str, limit: int) -> str:
    """在 Python 字符边界截断，并使用固定标记表达省略。"""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return TRUNCATED_VALUE
    if len(value) <= limit:
        return value
    if limit <= len(TRUNCATED_VALUE):
        return TRUNCATED_VALUE
    return f"{value[:limit - len(TRUNCATED_VALUE)]}{TRUNCATED_VALUE}"


def _known_roots() -> tuple[tuple[str, str], ...]:
    """构造不触发运行时组件的已知路径根目录替换表。"""
    roots: list[tuple[str, str]] = []
    for value, placeholder in (
        (hermes_home(), "<HERMES_HOME>"),
        (Path.home(), "<HOME>"),
        (PROJECT_ROOT, "<PROJECT_ROOT>"),
        (Path(tempfile.gettempdir()), "<TEMP>"),
    ):
        text = str(value)
        if text and (text, placeholder) not in roots:
            roots.append((text, placeholder))
    return tuple(roots)


def _path_variants(value: str) -> tuple[str, ...]:
    """同时识别正斜杠和反斜杠形式，避免依赖当前操作系统。"""
    variants = {value, value.replace("\\", "/"), value.replace("/", "\\")}
    return tuple(sorted((item for item in variants if item), key=len, reverse=True))


def _is_windows_style(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", value)) or "\\" in value
