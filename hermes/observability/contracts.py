"""可由工具、Hook 与未来运行时模块共同使用的中立可观测契约。"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol
from urllib.parse import urlsplit

from hermes.redaction import redact_explicit_secrets


_MAX_METADATA_ITEMS = 256
_MAX_METADATA_TEXT_CHARS = 16_384
_MAX_METADATA_TAGS = 16
_MAX_METADATA_TAG_LENGTH = 64
_MAX_METADATA_URL_LENGTH = 512
_SENSITIVE_METADATA_KEYS = frozenset({
    "access_key",
    "api_key",
    "apikey",
    "client_secret",
    "private_key",
    "refresh_token",
})
_SENSITIVE_METADATA_KEY_PARTS = frozenset({
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
})
_DISALLOWED_METADATA_KEY_PARTS = frozenset({
    "arguments",
    "content",
    "exception",
    "message",
    "messages",
    "model_response",
    "prompt",
    "raw_response",
    "response",
    "result",
    "stacktrace",
    "traceback",
})
_ERROR_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_WINDOWS_DRIVE_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_PRIVATE_KEY_TEXT_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
    re.IGNORECASE,
)
_MULTI_LINE_OR_CONTROL_TEXT_RE = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u2028\u2029]"
)
_OBVIOUS_CREDENTIAL_TEXT_RE = re.compile(
    r"(?:"
    r"(?i:\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,})"
    r"|(?<![A-Za-z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,})(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])AIza[A-Za-z0-9_-]{35}(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])(?:xox[baprs]-|glpat-)"
    r"[A-Za-z0-9_-]{12,}(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}"
    r"\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"(?![A-Za-z0-9_-])"
    r")"
)
_SAFE_METADATA_TEXT_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$"
)
_SAFE_METADATA_TAG_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}$"
)
_URI_OR_URL_LIKE_TEXT_RE = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9+.-]*:|https?(?:/+|\\+))",
    re.IGNORECASE,
)
_DNS_LABEL_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
_AMBIGUOUS_NUMERIC_HOST_RE = re.compile(
    r"^(?:0[xX][A-Fa-f0-9]+|[0-9]+)"
    r"(?:\.(?:0[xX][A-Fa-f0-9]+|[0-9]+))*$"
)
_SAFE_URL_PATH_RE = re.compile(
    r"^(?:/[A-Za-z0-9._~!$&'()*+,;=:@+-]*)*$"
)

_RUNTIME_METADATA_FIELDS = frozenset({
    "adapter",
    "attempt_count",
    "capability",
    "component_version",
    "enabled",
    "environment",
    "feature",
    "mode",
    "phase",
    "provider",
    "queue_depth",
    "reason_code",
    "region",
    "retry_count",
    "role",
    "service",
    "state",
    "status_code",
    "tags",
    "version",
    "worker_count",
})
_ARTIFACT_METADATA_FIELDS = frozenset({
    "artifact_kind",
    "checksum_algorithm",
    "category",
    "format",
    "has_storage_ref",
    "operation",
    "producer",
    "size_bytes",
    "source",
    "status_code",
    "tags",
    "type",
})
_RUNTIME_BOOLEAN_METADATA_FIELDS = frozenset({"enabled"})
_RUNTIME_INTEGER_METADATA_FIELDS = frozenset({
    "attempt_count",
    "queue_depth",
    "retry_count",
    "status_code",
    "worker_count",
})
_ARTIFACT_BOOLEAN_METADATA_FIELDS = frozenset({"has_storage_ref"})
_ARTIFACT_INTEGER_METADATA_FIELDS = frozenset({
    "size_bytes",
    "status_code",
})


def _require_text(value: object, field_name: str) -> str:
    """校验契约中的稳定文本身份字段。"""
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, field_name: str) -> str | None:
    """校验可选文本，避免将任意对象表示写入不可变契约。"""
    if value is None:
        return None
    return _require_text(value, field_name)


def _optional_error_type(value: object, field_name: str) -> str | None:
    """只接收稳定错误类型名，避免把异常正文作为 error_type 进入契约。"""
    if value is None:
        return None
    text = _require_text(value, field_name)
    if not _ERROR_TYPE_RE.fullmatch(text):
        raise ValueError(f"{field_name} must be an error type name")
    return text


def _nonnegative_int(value: object, field_name: str) -> int:
    """校验统计字段，显式拒绝布尔值伪装的整数。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _normalize_string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    """将契约集合规范化为稳定排序的不可变字符串元组。"""
    if not isinstance(value, (tuple, list, set, frozenset)):
        raise TypeError(f"{field_name} must be a collection of strings")
    normalized = {_require_text(item, field_name) for item in value}
    return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class _MetadataDomainRules:
    """一个领域专用 metadata 入口的固定字段类型规则。"""

    allowed_fields: frozenset[str]
    boolean_fields: frozenset[str]
    integer_fields: frozenset[str]


_RUNTIME_METADATA_RULES = _MetadataDomainRules(
    allowed_fields=_RUNTIME_METADATA_FIELDS,
    boolean_fields=_RUNTIME_BOOLEAN_METADATA_FIELDS,
    integer_fields=_RUNTIME_INTEGER_METADATA_FIELDS,
)
_ARTIFACT_METADATA_RULES = _MetadataDomainRules(
    allowed_fields=_ARTIFACT_METADATA_FIELDS,
    boolean_fields=_ARTIFACT_BOOLEAN_METADATA_FIELDS,
    integer_fields=_ARTIFACT_INTEGER_METADATA_FIELDS,
)


def _freeze_domain_metadata(
    value: Mapping[str, object],
    *,
    field_name: str,
    rules: _MetadataDomainRules,
) -> Mapping[str, object]:
    """按内部固定领域规则复制并冻结有限摘要元数据。"""
    if type(value) is not dict and not isinstance(value, MappingProxyType):
        raise TypeError(f"{field_name} must be a plain mapping")
    budget = _MetadataBudget(
        remaining_items=_MAX_METADATA_ITEMS,
        remaining_text_chars=_MAX_METADATA_TEXT_CHARS,
    )
    frozen: dict[str, object] = {}
    normalized_keys: set[str] = set()
    for key, child in value.items():
        if type(key) is not str:
            raise TypeError(f"{field_name} mapping keys must be strings")
        normalized_key = _validate_metadata_key(
            key,
            field_name,
            rules.allowed_fields,
        )
        if normalized_key in normalized_keys:
            raise ValueError(
                f"{field_name} contains duplicate normalized keys"
            )
        normalized_keys.add(normalized_key)
        budget.consume_item(field_name)
        budget.consume_text(key, field_name)
        frozen[key] = _freeze_metadata_field(
            child,
            f"{field_name}.{key}",
            normalized_key,
            rules,
            budget,
        )
    return MappingProxyType(frozen)


def freeze_runtime_metadata(value: Mapping[str, object]) -> Mapping[str, object]:
    """冻结 Runtime Snapshot 允许发布的摘要字段。"""
    return _freeze_domain_metadata(
        value,
        field_name="metadata",
        rules=_RUNTIME_METADATA_RULES,
    )


def freeze_artifact_metadata(value: Mapping[str, object]) -> Mapping[str, object]:
    """冻结 Artifact Record 允许发布的摘要字段。"""
    return _freeze_domain_metadata(
        value,
        field_name="metadata",
        rules=_ARTIFACT_METADATA_RULES,
    )


@dataclass(slots=True)
class _MetadataBudget:
    """限制 metadata 的总结构和文本规模，避免契约对象承载未界定载荷。"""

    remaining_items: int
    remaining_text_chars: int

    def consume_item(self, path: str) -> None:
        if self.remaining_items <= 0:
            raise ValueError(f"{path} exceeds the metadata item limit")
        self.remaining_items -= 1

    def consume_text(self, value: str, path: str) -> None:
        if len(value) > self.remaining_text_chars:
            raise ValueError(f"{path} exceeds the metadata text limit")
        self.remaining_text_chars -= len(value)


def _freeze_metadata_field(
    value: object,
    path: str,
    normalized_key: str,
    rules: _MetadataDomainRules,
    budget: _MetadataBudget,
) -> object:
    """按字段规则拒绝隐式类型转换和任意嵌套载荷。"""
    if normalized_key in rules.boolean_fields:
        if type(value) is not bool:
            raise TypeError(f"{path} must be a boolean")
        return value
    if normalized_key in rules.integer_fields:
        if type(value) is not int:
            raise TypeError(f"{path} must be a non-negative integer")
        if value < 0:
            raise ValueError(f"{path} must be a non-negative integer")
        return value
    if normalized_key == "tags":
        return _freeze_metadata_tags(value, path, budget)
    if type(value) is not str:
        raise TypeError(f"{path} must be a string")
    return _freeze_metadata_text(value, path, budget)


def _normalize_metadata_key(key: str) -> str:
    """统一分隔大小写和符号混写的 metadata 字段名。"""
    separated = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", key)
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", separated)
    return re.sub(r"[^a-z0-9]+", "_", separated.lower()).strip("_")


def _freeze_metadata_tags(
    value: object,
    path: str,
    budget: _MetadataBudget,
) -> tuple[str, ...]:
    """冻结数量和内容受限的稳定标签集合。"""
    if type(value) not in (list, tuple):
        raise TypeError(f"{path} must be a list or tuple of tags")
    if len(value) > _MAX_METADATA_TAGS:
        raise ValueError(f"{path} exceeds the metadata tag limit")
    frozen: list[str] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        budget.consume_item(item_path)
        if type(item) is not str:
            raise TypeError(f"{item_path} must be a string")
        if (
            not item
            or item != item.strip()
            or len(item) > _MAX_METADATA_TAG_LENGTH
            or not _SAFE_METADATA_TAG_RE.fullmatch(item)
        ):
            raise ValueError(f"{item_path} must be a compact metadata tag")
        _reject_sensitive_metadata_text(item, item_path)
        budget.consume_text(item, item_path)
        frozen.append(item)
    return tuple(frozen)


def _freeze_metadata_text(
    value: str,
    path: str,
    budget: _MetadataBudget,
) -> str:
    """只保留短的单行摘要标签或经严格解析的安全 URL。"""
    if not value or value != value.strip():
        raise ValueError(f"{path} must contain a compact metadata label")
    _reject_sensitive_metadata_text(value, path)
    if _URI_OR_URL_LIKE_TEXT_RE.match(value):
        _validate_metadata_url(value, path)
    elif _is_absolute_or_device_path(value):
        raise ValueError(f"{path} must not contain an absolute path")
    elif not _SAFE_METADATA_TEXT_RE.fullmatch(value):
        raise ValueError(f"{path} must contain a compact metadata label")
    budget.consume_text(value, path)
    return value


def _reject_sensitive_metadata_text(value: str, path: str) -> None:
    """拒绝多行、私钥和高置信凭证文本。"""
    if _MULTI_LINE_OR_CONTROL_TEXT_RE.search(value):
        raise ValueError(f"{path} must not contain multi-line text")
    if _PRIVATE_KEY_TEXT_RE.search(value):
        raise ValueError(f"{path} must not contain private key text")
    if (
        redact_explicit_secrets(value) != value
        or _OBVIOUS_CREDENTIAL_TEXT_RE.search(value)
    ):
        raise ValueError(f"{path} must not contain credential text")


def _is_absolute_or_device_path(value: str) -> bool:
    """识别 POSIX、UNC、盘符绝对路径和 Windows device path。"""
    if value.startswith(("/", "\\")):
        return True
    if _WINDOWS_DRIVE_ABSOLUTE_PATH_RE.match(value):
        return True
    normalized = value.replace("/", "\\")
    return normalized.startswith(("\\\\?\\", "\\\\.\\", "\\??\\"))


def _validate_metadata_url(value: str, path: str) -> None:
    """只接收无凭证、查询和片段的可确定 HTTP(S) URL。"""
    if len(value) > _MAX_METADATA_URL_LENGTH or "\\" in value:
        raise ValueError(f"{path} contains an invalid URL")
    try:
        parsed = urlsplit(value)
        username = parsed.username
        password = parsed.password
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError(f"{path} contains an invalid URL") from None

    scheme = parsed.scheme.lower()
    if (
        scheme not in {"http", "https"}
        or not value.lower().startswith(f"{scheme}://")
        or not parsed.netloc
        or not hostname
    ):
        raise ValueError(f"{path} contains an invalid URL")
    if username is not None or password is not None:
        raise ValueError(f"{path} URL must not contain user information")
    if "?" in value or parsed.query:
        raise ValueError(f"{path} URL must not contain a query")
    if "#" in value or parsed.fragment:
        raise ValueError(f"{path} URL must not contain a fragment")
    if port is not None and not 1 <= port <= 65_535:
        raise ValueError(f"{path} URL contains an invalid port")
    if not _SAFE_URL_PATH_RE.fullmatch(parsed.path):
        raise ValueError(f"{path} contains an invalid URL")

    authority = parsed.netloc
    bracketed_host = authority.startswith("[")
    if bracketed_host:
        closing_bracket = authority.find("]")
        suffix = authority[closing_bracket + 1:]
        if closing_bracket < 0 or (
            suffix
            and (
                not suffix.startswith(":")
                or not suffix[1:]
                or not suffix[1:].isdigit()
            )
        ):
            raise ValueError(f"{path} contains an invalid URL")
    else:
        if authority.count(":") > 1:
            raise ValueError(f"{path} contains an invalid URL")
        if ":" in authority and not authority.rsplit(":", 1)[1]:
            raise ValueError(f"{path} URL contains an invalid port")

    _validate_metadata_hostname(hostname, path, bracketed_host)


def _validate_metadata_hostname(
    hostname: str,
    path: str,
    bracketed: bool,
) -> None:
    """在不执行 DNS 查询的前提下校验 IP 或 DNS hostname。"""
    if "%" in hostname:
        raise ValueError(f"{path} contains an invalid URL hostname")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if bracketed:
            raise ValueError(
                f"{path} contains an invalid URL hostname"
            ) from None
    else:
        if bracketed and address.version != 6:
            raise ValueError(f"{path} contains an invalid URL hostname")
        return

    if _AMBIGUOUS_NUMERIC_HOST_RE.fullmatch(hostname):
        raise ValueError(f"{path} contains an invalid URL hostname")
    if not hostname.isascii():
        raise ValueError(f"{path} contains an invalid URL hostname")
    ascii_hostname = hostname
    if (
        not ascii_hostname
        or len(ascii_hostname) > 253
        or ascii_hostname.startswith(".")
        or ascii_hostname.endswith(".")
        or any(
            not _DNS_LABEL_RE.fullmatch(label)
            for label in ascii_hostname.split(".")
        )
    ):
        raise ValueError(f"{path} contains an invalid URL hostname")


def _validate_metadata_key(
    key: str,
    path: str,
    allowed_fields: frozenset[str],
) -> str:
    """仅允许白名单摘要键，并按组成部分拒绝正文和凭证承载字段。"""
    normalized = _normalize_metadata_key(key)
    if not normalized:
        raise ValueError(f"{path} contains an invalid metadata key")
    parts = frozenset(part for part in normalized.split("_") if part)
    if (
        normalized in _SENSITIVE_METADATA_KEYS
        or parts & _SENSITIVE_METADATA_KEY_PARTS
        or parts & _DISALLOWED_METADATA_KEY_PARTS
    ):
        raise ValueError(f"{path} contains a sensitive metadata key")
    if key != normalized:
        raise ValueError(f"{path} contains a non-canonical metadata key")
    if normalized not in allowed_fields:
        raise ValueError(f"{path} contains a metadata key outside the allowed set")
    return normalized


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """单个工具声明的不可执行、不可变能力摘要。"""

    name: str
    toolset: str
    description: str
    parameter_names: tuple[str, ...]
    required_parameters: tuple[str, ...]
    execution_environments: tuple[str, ...]
    default_enabled_environments: tuple[str, ...]
    unattended_allowed: bool
    approval_mode: str
    risk_level: str
    retry_safe: bool
    unknown_on_crash: bool
    supports_cancellation: bool
    has_status_check: bool

    def __post_init__(self) -> None:
        """复制集合字段，确保描述不持有 Registry 的可变 schema。"""
        object.__setattr__(self, "name", _require_text(self.name, "name"))
        object.__setattr__(self, "toolset", _require_text(self.toolset, "toolset"))
        if not isinstance(self.description, str):
            raise TypeError("description must be a string")
        object.__setattr__(
            self,
            "parameter_names",
            _normalize_string_tuple(self.parameter_names, "parameter_names"),
        )
        object.__setattr__(
            self,
            "required_parameters",
            _normalize_string_tuple(self.required_parameters, "required_parameters"),
        )
        object.__setattr__(
            self,
            "execution_environments",
            _normalize_string_tuple(
                self.execution_environments,
                "execution_environments",
            ),
        )
        object.__setattr__(
            self,
            "default_enabled_environments",
            _normalize_string_tuple(
                self.default_enabled_environments,
                "default_enabled_environments",
            ),
        )
        for field_name in (
            "unattended_allowed",
            "retry_safe",
            "unknown_on_crash",
            "supports_cancellation",
            "has_status_check",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(f"{field_name} must be a boolean")
        object.__setattr__(
            self,
            "approval_mode",
            _require_text(self.approval_mode, "approval_mode"),
        )
        object.__setattr__(
            self,
            "risk_level",
            _require_text(self.risk_level, "risk_level"),
        )


@dataclass(frozen=True, slots=True)
class ToolsetDescriptor:
    """按工具集聚合后的不可执行能力摘要。"""

    name: str
    tool_names: tuple[str, ...]
    execution_environments: tuple[str, ...]
    default_enabled_environments: tuple[str, ...]
    available: bool = True

    def __post_init__(self) -> None:
        """规范化所有聚合集合的排序和不可变性。"""
        object.__setattr__(self, "name", _require_text(self.name, "name"))
        object.__setattr__(
            self,
            "tool_names",
            _normalize_string_tuple(self.tool_names, "tool_names"),
        )
        object.__setattr__(
            self,
            "execution_environments",
            _normalize_string_tuple(
                self.execution_environments,
                "execution_environments",
            ),
        )
        object.__setattr__(
            self,
            "default_enabled_environments",
            _normalize_string_tuple(
                self.default_enabled_environments,
                "default_enabled_environments",
            ),
        )
        if not isinstance(self.available, bool):
            raise TypeError("available must be a boolean")


@dataclass(frozen=True, slots=True)
class ToolCallObservation:
    """不含工具参数和结果的单次工具调用观察事件。"""

    observation_id: str
    run_id: str
    parent_run_id: str | None
    tool_call_id: str
    tool_name: str
    status: str
    success: bool
    error_type: str | None
    duration_ms: int

    def __post_init__(self) -> None:
        """校验 Hook 适配后的最小安全字段。"""
        for field_name in (
            "observation_id",
            "run_id",
            "tool_call_id",
            "tool_name",
            "status",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "parent_run_id",
            _optional_text(self.parent_run_id, "parent_run_id"),
        )
        object.__setattr__(
            self,
            "error_type",
            _optional_error_type(self.error_type, "error_type"),
        )
        if not isinstance(self.success, bool):
            raise TypeError("success must be a boolean")
        object.__setattr__(
            self,
            "duration_ms",
            _nonnegative_int(self.duration_ms, "duration_ms"),
        )


@dataclass(frozen=True, slots=True)
class ModelCallObservation:
    """不含 Prompt 或模型正文的单次模型调用观察事件。"""

    observation_id: str
    run_id: str
    parent_run_id: str | None
    finish_reason: str | None
    has_text: bool
    tool_call_count: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    duration_ms: int

    def __post_init__(self) -> None:
        """校验模型观察仅保存统计信息。"""
        object.__setattr__(
            self,
            "observation_id",
            _require_text(self.observation_id, "observation_id"),
        )
        object.__setattr__(self, "run_id", _require_text(self.run_id, "run_id"))
        for field_name in ("parent_run_id", "finish_reason"):
            object.__setattr__(
                self,
                field_name,
                _optional_text(getattr(self, field_name), field_name),
            )
        if not isinstance(self.has_text, bool):
            raise TypeError("has_text must be a boolean")
        for field_name in (
            "tool_call_count",
            "duration_ms",
        ):
            object.__setattr__(
                self,
                field_name,
                _nonnegative_int(getattr(self, field_name), field_name),
            )
        for field_name in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
        ):
            value = getattr(self, field_name)
            if value is not None:
                value = _nonnegative_int(value, field_name)
            object.__setattr__(self, field_name, value)


@dataclass(frozen=True, slots=True)
class RunObservation:
    """不含最终回复正文的一次运行结束观察事件。"""

    observation_id: str
    run_id: str
    parent_run_id: str | None
    status: str
    stop_reason: str
    iterations: int
    tool_call_count: int
    has_final_reply: bool

    def __post_init__(self) -> None:
        """校验运行事件的稳定关联和统计字段。"""
        for field_name in (
            "observation_id",
            "run_id",
            "status",
            "stop_reason",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "parent_run_id",
            _optional_text(self.parent_run_id, "parent_run_id"),
        )
        for field_name in ("iterations", "tool_call_count"):
            object.__setattr__(
                self,
                field_name,
                _nonnegative_int(getattr(self, field_name), field_name),
            )
        if not isinstance(self.has_final_reply, bool):
            raise TypeError("has_final_reply must be a boolean")


class ObservationSink(Protocol):
    """旁路观察事件的同步接收端。"""

    def record_tool_call(self, observation: ToolCallObservation) -> None:
        """接收单次工具调用观察。"""

    def record_model_call(self, observation: ModelCallObservation) -> None:
        """接收单次模型调用观察。"""

    def record_run_end(self, observation: RunObservation) -> None:
        """接收一次运行结束观察。"""


class NullObservationSink:
    """不保存状态且永不主动抛错的空观察接收端。"""

    __slots__ = ()

    def record_tool_call(self, observation: ToolCallObservation) -> None:
        del observation

    def record_model_call(self, observation: ModelCallObservation) -> None:
        del observation

    def record_run_end(self, observation: RunObservation) -> None:
        del observation
