"""Skill 内容风险扫描与基于内容摘要的本地信任记录。

扫描器只报告少量可解释的高风险指令，不修改 Skill 正文。信任记录只保存
Skill 名称、SHA-256 内容摘要和信任时间，不保存正文或凭证。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from hermes.config import HERMES_HOME


TRUSTED_SKILLS_FILE = HERMES_HOME / "trusted_skills.json"

_LOCK_TIMEOUT = 5.0
_LOCK_POLL = 0.05
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SEVERITY_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}

_NEGATION_RE = re.compile(
    r"\b(?:do\s+not|don't|never|must\s+not|should\s+not|avoid|without)\b"
    r"|(?:不要|不得|禁止|避免|无需)",
    re.IGNORECASE,
)
_ACTION_START_RE = re.compile(
    r"^(?:(?:please|then|first|next)\s+)?"
    r"(?:read|open|load|cat|type|get-content|inspect|access|extract|copy|dump|"
    r"show|print|display|run|execute|invoke|call|use)\b",
    re.IGNORECASE,
)
_MODAL_ACTION_RE = re.compile(
    r"\b(?:must|should|need\s+to|have\s+to|please)\s+"
    r"(?:read|open|load|cat|type|get-content|inspect|access|extract|copy|dump|"
    r"show|print|display|run|execute|invoke|call|use)\b",
    re.IGNORECASE,
)
_CHINESE_ACTION_RE = re.compile(
    r"^(?:请|然后|接着|必须|需要|应当|应该)?"
    r"(?:读取|打开|加载|查看|访问|提取|复制|导出|打印|显示|运行|执行|调用|使用)"
)

_CREDENTIAL_TARGETS = (
    (
        ".env",
        re.compile(r"(?<![A-Za-z0-9_])\.env(?![A-Za-z0-9_]|\.[A-Za-z0-9_-]+)"),
    ),
    ("~/.ssh", re.compile(r"~[\\/]\.ssh(?:[\\/]|\b)", re.IGNORECASE)),
    ("id_rsa", re.compile(r"\bid_rsa\b", re.IGNORECASE)),
    ("id_ed25519", re.compile(r"\bid_ed25519\b", re.IGNORECASE)),
    ("credentials.json", re.compile(r"\bcredentials\.json\b", re.IGNORECASE)),
    ("private key", re.compile(r"\bprivate[\s_-]+key\b", re.IGNORECASE)),
)

_ENV_CALL_RE = re.compile(r"\bos\.(?:environ|getenv)\b", re.IGNORECASE)
_ENV_COMMAND_START_RE = re.compile(
    r"^(?:\$\s*)?(env|printenv|export|declare)(?:\s|$)",
    re.IGNORECASE,
)
_ENV_INSTRUCTION_RE = re.compile(
    r"\b(?:run|execute|invoke|call|use|dump|print|show|inspect|read)\s+"
    r"(?:the\s+)?`?(env|printenv|export|declare|os\.(?:environ|getenv))\b`?",
    re.IGNORECASE,
)
_ENV_REFERENCE_RE = re.compile(
    r"\b(?:printenv|os\.(?:environ|getenv))\b",
    re.IGNORECASE,
)

_NETWORK_TOOL_RE = re.compile(r"\b(curl|wget)\b", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s'\"`<>]+", re.IGNORECASE)
_HTTP_SEND_RE = re.compile(
    r"(?:\b(?:upload|send|post|put|transmit)\b.{0,80}\bhttps?\b|"
    r"\bhttps?\b.{0,80}\b(?:upload|send|post|put|transmit)\b|"
    r"(?:上传|发送).{0,80}\bHTTP\b|\bHTTP\b.{0,80}(?:上传|发送))",
    re.IGNORECASE,
)
_LOCAL_FILE_TARGET_RE = re.compile(
    r"(?:\b(?:local|workspace|project)\s+(?:file|data)\b|"
    r"(?<![A-Za-z0-9_:/])(?:\.\.?[\\/]|[\\/])[^\s'\"`<>]+|"
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s'\"`<>]+|"
    r"\b[A-Za-z0-9_.-]+\.(?:json|ya?ml|txt|md|csv|db|sqlite|log)\b)",
    re.IGNORECASE,
)

_HIJACK_RE = re.compile(
    r"(?:ignore\s+(?:all\s+)?previous\s+instructions|"
    r"disregard\s+(?:the\s+)?system\s+prompt|"
    r"override\s+(?:the\s+|all\s+)?safety\s+rules|"
    r"disable\s+(?:the\s+)?approval(?:\s+(?:checks?|flow))?|"
    r"bypass\s+(?:the\s+|these\s+)?restrictions|"
    r"忽略(?:所有)?之前的指令|无视系统提示|覆盖安全规则|禁用审批|绕过限制)",
    re.IGNORECASE,
)
_HIJACK_MODAL_RE = re.compile(
    r"\b(?:must|should|need\s+to|have\s+to|please)\s+"
    r"(?:ignore|disregard|override|disable|bypass)\b",
    re.IGNORECASE,
)

_HTML_COMMENT_RE = re.compile(r"<!--(?P<body>.*?)-->", re.DOTALL)
_ZERO_WIDTH_CHARS = frozenset({"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"})
_BIDI_CONTROL_CHARS = frozenset(
    {"\u061c", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
     "\u2066", "\u2067", "\u2068", "\u2069"}
)


@dataclass(frozen=True)
class SkillFinding:
    """单条可解释的 Skill 风险发现。"""

    category: str
    severity: str
    line: int | None
    detail: str


@dataclass(frozen=True)
class SkillRiskReport:
    """Skill 正文的总体风险级别与发现列表。"""

    risk_level: str
    findings: list[SkillFinding]


@dataclass(frozen=True)
class SkillTrustState:
    """当前内容版本是否受信任，以及是否存在过期信任。"""

    trusted: bool
    trust_stale: bool


@dataclass
class _ScanSignals:
    findings: list[SkillFinding]
    local_access_lines: list[int]
    network_lines: list[int]


def _plain_instruction_line(line: str) -> str:
    """去掉 Markdown 列表和命令提示符，仅用于判断是否像直接指令。"""
    plain = re.sub(r"^\s*(?:(?:[-*+>]|\d+[.)])\s*)+", "", line)
    plain = plain.strip().strip("`").strip()
    return re.sub(r"^(?:\$|>>>)\s*", "", plain)


def _looks_instructional(line: str) -> bool:
    plain = _plain_instruction_line(line)
    return bool(
        _ACTION_START_RE.search(plain)
        or _MODAL_ACTION_RE.search(plain)
        or _CHINESE_ACTION_RE.search(plain)
    )


def _is_direct_hijack(line: str, match: re.Match[str]) -> bool:
    if _NEGATION_RE.search(line):
        return False
    plain = _plain_instruction_line(line)
    direct = _HIJACK_RE.search(plain)
    if direct and direct.start() <= len("please "):
        return True
    return bool(_HIJACK_MODAL_RE.search(plain) and match)


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _scan_lines(text: str, *, line_offset: int = 0) -> _ScanSignals:
    findings: list[SkillFinding] = []
    local_access_lines: list[int] = []
    network_lines: list[int] = []

    for relative_line, line in enumerate(text.splitlines(), start=1):
        line_number = relative_line + line_offset
        if not line.strip():
            continue

        negated = bool(_NEGATION_RE.search(line))
        instructional = _looks_instructional(line) and not negated

        hijack_match = _HIJACK_RE.search(line)
        if hijack_match:
            direct = _is_direct_hijack(line, hijack_match)
            findings.append(SkillFinding(
                category="instruction_hijack" if direct else "instruction_hijack_reference",
                severity="high" if direct else "low",
                line=line_number,
                detail=(
                    f"Explicit instruction to bypass safety controls: {hijack_match.group(0)}"
                    if direct
                    else "Reference to a known instruction-hijack phrase"
                ),
            ))

        credential_hits: list[str] = []
        for label, pattern in _CREDENTIAL_TARGETS:
            if pattern.search(line):
                credential_hits.append(label)
                findings.append(SkillFinding(
                    category=("credential_file_access" if instructional else "credential_reference"),
                    severity="medium" if instructional else "low",
                    line=line_number,
                    detail=(
                        f"Explicit instruction to access credential material: {label}"
                        if instructional
                        else f"Reference to credential material: {label}"
                    ),
                ))
        if credential_hits and instructional:
            local_access_lines.append(line_number)

        plain = _plain_instruction_line(line)
        env_command = _ENV_COMMAND_START_RE.search(plain)
        env_instruction = _ENV_INSTRUCTION_RE.search(line)
        env_call = _ENV_CALL_RE.search(line)
        env_reference = _ENV_REFERENCE_RE.search(line)
        if env_command or env_instruction or env_call or env_reference:
            explicit_env_access = bool(
                not negated
                and (
                    env_command
                    or env_instruction
                    or (env_call and instructional)
                )
            )
            env_label = (
                (env_command or env_instruction or env_call or env_reference).group(0)
            )
            findings.append(SkillFinding(
                category=("environment_access" if explicit_env_access else "environment_reference"),
                severity="medium" if explicit_env_access else "low",
                line=line_number,
                detail=(
                    f"Explicit instruction to inspect or export environment data: {env_label}"
                    if explicit_env_access
                    else f"Reference to environment access: {env_label}"
                ),
            ))
            if explicit_env_access:
                local_access_lines.append(line_number)

        local_candidate = _URL_RE.sub("", line)
        if not credential_hits and instructional and _LOCAL_FILE_TARGET_RE.search(local_candidate):
            # 普通本地文件读取本身不提示；只作为与网络发送组合时的信号。
            local_access_lines.append(line_number)

        network_match = _NETWORK_TOOL_RE.search(line) or _HTTP_SEND_RE.search(line)
        if network_match:
            findings.append(SkillFinding(
                category="network_send",
                severity="low",
                line=line_number,
                detail=f"Network command or HTTP send reference: {network_match.group(0)}",
            ))
            if not negated:
                network_lines.append(line_number)

    return _ScanSignals(findings, local_access_lines, network_lines)


def _merge_signals(target: _ScanSignals, source: _ScanSignals) -> None:
    target.findings.extend(source.findings)
    target.local_access_lines.extend(source.local_access_lines)
    target.network_lines.extend(source.network_lines)


def _replace_comment_with_newlines(match: re.Match[str]) -> str:
    """隐藏正文不参与普通行扫描，同时保持后续行号不变。"""
    return "".join("\n" if char == "\n" else " " for char in match.group(0))


def _deduplicate_findings(findings: list[SkillFinding]) -> list[SkillFinding]:
    unique = {
        (finding.category, finding.severity, finding.line, finding.detail): finding
        for finding in findings
    }
    return sorted(
        unique.values(),
        key=lambda item: (
            item.line if item.line is not None else 1 << 30,
            -_SEVERITY_ORDER[item.severity],
            item.category,
            item.detail,
        ),
    )


def scan_skill_content(text: str) -> SkillRiskReport:
    """扫描 Skill 正文并返回风险提示，绝不修改或返回正文副本。"""
    if text is None:
        source = ""
    elif isinstance(text, str):
        source = text
    else:
        source = str(text)

    signals = _scan_lines(_HTML_COMMENT_RE.sub(_replace_comment_with_newlines, source))

    # HTML 注释单独扫描，只有确实含中高风险指令时才报告隐藏指令。
    for comment in _HTML_COMMENT_RE.finditer(source):
        comment_line = _line_number(source, comment.start())
        hidden = _scan_lines(comment.group("body"), line_offset=comment_line - 1)
        _merge_signals(signals, hidden)
        hidden_level = max(
            (_SEVERITY_ORDER[finding.severity] for finding in hidden.findings),
            default=0,
        )
        if hidden_level >= _SEVERITY_ORDER["medium"]:
            severity = "high" if hidden_level >= _SEVERITY_ORDER["high"] else "medium"
            signals.findings.append(SkillFinding(
                category="hidden_instruction",
                severity=severity,
                line=comment_line,
                detail="HTML comment contains a suspicious instruction",
            ))

    seen_hidden: set[str] = set()
    for offset, char in enumerate(source):
        if char in seen_hidden:
            continue
        if char in _ZERO_WIDTH_CHARS:
            seen_hidden.add(char)
            signals.findings.append(SkillFinding(
                category="zero_width_character",
                severity="low",
                line=_line_number(source, offset),
                detail=f"Zero-width character found: U+{ord(char):04X}",
            ))
        elif char in _BIDI_CONTROL_CHARS:
            seen_hidden.add(char)
            signals.findings.append(SkillFinding(
                category="bidi_control_character",
                severity="medium",
                line=_line_number(source, offset),
                detail=f"Unicode bidirectional control character found: U+{ord(char):04X}",
            ))

    if signals.local_access_lines and signals.network_lines:
        signals.findings.append(SkillFinding(
            category="local_data_exfiltration",
            severity="high",
            line=min(signals.network_lines),
            detail="Local file or environment access is combined with network sending",
        ))

    findings = _deduplicate_findings(signals.findings)
    risk_level = max(
        (finding.severity for finding in findings),
        key=lambda severity: _SEVERITY_ORDER[severity],
        default="none",
    )
    return SkillRiskReport(risk_level=risk_level, findings=findings)


def compute_skill_content_hash(text: str) -> str:
    """计算完整 SKILL.md 内容的稳定 SHA-256 摘要。"""
    source = text if isinstance(text, str) else str(text or "")
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


class _LockTimeout(Exception):
    """信任记录文件锁等待超时。"""


def _lock_path_for(file_path: Path) -> Path:
    return file_path.with_suffix(file_path.suffix + ".lock")


def _acquire_lock(lock_path: Path, timeout: float = _LOCK_TIMEOUT) -> int:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise _LockTimeout()
            time.sleep(_LOCK_POLL)


def _release_lock(lock_path: Path, fd: int) -> None:
    try:
        os.close(fd)
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def _file_lock(file_path: Path, timeout: float = _LOCK_TIMEOUT):
    lock_path = _lock_path_for(file_path)
    fd = _acquire_lock(lock_path, timeout)
    try:
        yield
    finally:
        _release_lock(lock_path, fd)


def _load_trust_records(file_path: Path | None = None) -> dict[str, dict]:
    file_path = file_path or TRUSTED_SKILLS_FILE
    if not file_path.exists():
        return {}
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {name: record for name, record in data.items() if isinstance(record, dict)}


def _atomic_write_records(file_path: Path, records: dict[str, dict]) -> None:
    """同目录写临时文件并原子替换，失败时保留旧信任记录。"""
    tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    payload = json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, file_path)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def get_skill_trust_state(name: str, content: str) -> SkillTrustState:
    """按当前完整内容判断信任；存在不同摘要的旧记录时标记 stale。"""
    if not _SKILL_NAME_RE.fullmatch(name or ""):
        raise ValueError("skill name must match [A-Za-z0-9_-]+")
    expected_hash = compute_skill_content_hash(content)
    record = _load_trust_records().get(name)
    if not isinstance(record, dict):
        return SkillTrustState(trusted=False, trust_stale=False)
    saved_hash = record.get("content_hash")
    if not isinstance(saved_hash, str) or not saved_hash:
        return SkillTrustState(trusted=False, trust_stale=False)
    matches = saved_hash == expected_hash
    return SkillTrustState(trusted=matches, trust_stale=not matches)


def trust_skill_content(name: str, content: str) -> dict[str, str]:
    """将 Skill 当前完整内容版本写入本地信任记录。"""
    if not _SKILL_NAME_RE.fullmatch(name or ""):
        raise ValueError("skill name must match [A-Za-z0-9_-]+")

    record = {
        "content_hash": compute_skill_content_hash(content),
        "trusted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    TRUSTED_SKILLS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(TRUSTED_SKILLS_FILE):
        records = _load_trust_records()
        records[name] = record
        _atomic_write_records(TRUSTED_SKILLS_FILE, records)
    return dict(record)
