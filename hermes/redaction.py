"""高置信度凭证值脱敏。"""

from __future__ import annotations

import re
from collections.abc import Iterable


__all__ = [
    "is_explicit_credential_env_name",
    "redact_explicit_secrets",
    "redact_file_content",
    "redact_terminal_output",
]


# 只识别现有的明确凭证形式，不根据普通关键词猜测敏感数据。
_EXPLICIT_VALUE_PATTERN = (
    r"(?:\$\{[A-Za-z_][A-Za-z0-9_]*\}"
    r"|<[A-Za-z_][A-Za-z0-9_.-]*>"
    r"|[^\s,;&)\]}>'\"]+)"
)
_AUTHORIZATION_BEARER_RE = re.compile(
    r"(?i)(?P<prefix>(?<![A-Za-z0-9_-])['\"]?Authorization['\"]?"
    r"\s*[:=]\s*['\"]?Bearer\s+)"
    rf"(?P<value>{_EXPLICIT_VALUE_PATTERN})"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?P<prefix>(?<![A-Za-z0-9_-])['\"]?"
    r"(?:[A-Za-z0-9]+[_-])*(?:api[_-]?key|token|password|secret)"
    r"['\"]?\s*[:=]\s*)(?P<quote>['\"]?)"
    rf"(?P<value>{_EXPLICIT_VALUE_PATTERN})"
)
_RAW_API_KEY_RE = re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{6,}")
_EXPLICIT_CREDENTIAL_ENV_NAME_RE = re.compile(
    r"(?i)^(?:[A-Za-z0-9]+[_-])*"
    r"(?:api[_-]?key|token|password|secret)$"
)
_PLACEHOLDER_VALUE_RE = re.compile(
    r"^(?:<secret>|\$\{[A-Za-z_][A-Za-z0-9_]*\}"
    r"|<[A-Za-z_][A-Za-z0-9_.-]*>)$",
    re.IGNORECASE,
)
_ENV_DUMP_COMMAND_RE = re.compile(
    r"^\s*(?:(?:command|builtin)\s+)?(?:/usr/bin/)?"
    r"(?:env|printenv|export|declare|set)"
    r"(?:\s+(?:--?[A-Za-z]+|"
    r"[A-Za-z_][A-Za-z0-9_]*(?:=[^\s]+)?))*\s*$"
)
_ENV_DUMP_ASSIGNMENT_RE = re.compile(
    r"(?m)^(?P<prefix>\s*(?:(?:declare|export)\s+"
    r"(?:--?[A-Za-z]+\s+)?)?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)=)"
    r"(?P<value>[^\r\n]*)$"
)


def is_explicit_credential_env_name(name: object) -> bool:
    """判断环境变量名是否以现有高置信度凭证字段结尾。"""
    if not isinstance(name, str):
        return False
    return bool(_EXPLICIT_CREDENTIAL_ENV_NAME_RE.fullmatch(name.strip()))


def _is_placeholder_value(value: str) -> bool:
    """识别结构明确的占位符，避免把配置模板改写成真实凭证。"""
    return bool(_PLACEHOLDER_VALUE_RE.fullmatch(value))


def _redact_authorization_match(match: re.Match) -> str:
    if _is_placeholder_value(match.group("value")):
        return match.group(0)
    return f"{match.group('prefix')}<secret>"


def _redact_assignment_match(match: re.Match) -> str:
    if _is_placeholder_value(match.group("value")):
        return match.group(0)
    return f"{match.group('prefix')}{match.group('quote')}<secret>"


def redact_explicit_secrets(text: object | None) -> str:
    """只替换明确凭证值，保留字段名、引号、标点和周边文本。"""
    if text is None:
        return ""
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return ""
    if not text:
        return text

    text = _AUTHORIZATION_BEARER_RE.sub(_redact_authorization_match, text)
    text = _SECRET_ASSIGNMENT_RE.sub(_redact_assignment_match, text)
    return _RAW_API_KEY_RE.sub("<secret>", text)


def _is_simple_environment_dump(command: object) -> bool:
    """只识别简单环境导出命令，不尝试解释管道、替换或完整 Shell 语义。"""
    if not isinstance(command, str):
        return False
    return bool(_ENV_DUMP_COMMAND_RE.fullmatch(command))


def _redact_environment_assignment(
    match: re.Match,
    infrastructure_env_names: frozenset[str],
) -> str:
    name = match.group("name")
    if (
        name.upper() not in infrastructure_env_names
        and not is_explicit_credential_env_name(name)
    ):
        return match.group(0)

    value = match.group("value")
    if not value:
        return match.group(0)
    leading = value[:len(value) - len(value.lstrip())]
    trailing = value[len(value.rstrip()):]
    core = value.strip()
    if not core:
        return match.group(0)

    quote = ""
    unquoted = core
    if len(core) >= 2 and core[0] in {"'", '"'} and core[-1] == core[0]:
        quote = core[0]
        unquoted = core[1:-1]
    if _is_placeholder_value(unquoted):
        return match.group(0)

    redacted = f"{quote}<secret>{quote}" if quote else "<secret>"
    return f"{match.group('prefix')}{leading}{redacted}{trailing}"


def redact_terminal_output(
    text: object | None,
    command: object = "",
    *,
    infrastructure_env_names: Iterable[str] = (),
) -> str:
    """处理 Terminal 返回副本；环境 dump 额外识别明确凭证变量名。"""
    redacted = redact_explicit_secrets(text)
    if not redacted or not _is_simple_environment_dump(command):
        return redacted
    protected_names = frozenset(
        str(name).strip().upper()
        for name in infrastructure_env_names
        if str(name).strip()
    )
    return _ENV_DUMP_ASSIGNMENT_RE.sub(
        lambda match: _redact_environment_assignment(
            match,
            protected_names,
        ),
        redacted,
    )


def redact_file_content(text: object | None) -> str:
    """处理 File 读取结果副本，不改变路径或磁盘内容。"""
    return redact_explicit_secrets(text)
