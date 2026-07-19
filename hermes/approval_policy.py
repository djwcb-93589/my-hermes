"""File / Terminal 的统一审批决策与操作指纹。"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shlex
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Protocol, Sequence

from hermes.file_state import normalize_file_state_snapshot
from hermes.path_policy import (
    PATH_POLICY_DENIED_ERROR_TYPE,
    PathAccessPolicy,
)


class ApprovalDecision(str, Enum):
    """审批策略允许的三种稳定决策。"""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class ApprovalRiskLevel(str, Enum):
    """审批展示和审计使用的四档风险级别。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


ALLOW = ApprovalDecision.ALLOW
ASK = ApprovalDecision.ASK
DENY = ApprovalDecision.DENY

LOW = ApprovalRiskLevel.LOW
MEDIUM = ApprovalRiskLevel.MEDIUM
HIGH = ApprovalRiskLevel.HIGH
CRITICAL = ApprovalRiskLevel.CRITICAL


@dataclass(frozen=True, slots=True)
class ApprovalAssessment:
    """一次规范化工具操作的审批结论与不可混用身份。"""

    tool_name: str
    decision: ApprovalDecision
    risk_level: ApprovalRiskLevel
    fingerprint: str
    reason: str
    normalized_arguments: dict
    details: dict
    normalized_command: str | None = None
    normalized_cwd: str | None = None
    normalized_path: str | None = None
    session_key: str | None = None
    error_type: str | None = None
    error: str | None = None
    fatal: bool = False


@dataclass(frozen=True, slots=True)
class TerminalCommandClassification:
    """保守静态分析得到的 Terminal 自动放行结论。"""

    automatically_allowed: bool
    operation_type: str
    risk_level: ApprovalRiskLevel
    reason: str
    target_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PolicyDenial:
    """统一策略在 grant 匹配前得到的不可审批拒绝。"""

    error_type: str
    reason: str
    error: str
    decision_source: str


@dataclass(frozen=True, slots=True)
class FileDenyRule:
    """用户配置的 File action 与路径组合拒绝规则。"""

    actions: frozenset[str]
    path_under: str


@dataclass(frozen=True, slots=True)
class BackendRiskAssessment:
    """提供给统一策略的最小 backend 风险画像。"""

    backend_type: str
    risk_floor: ApprovalRiskLevel
    automatic_allowance: bool
    reason: str


class IntelligentApprovalAdvisor(Protocol):
    """未来智能审批实现必须遵守的最小只读接口。"""

    def assess(self, assessment: ApprovalAssessment) -> ApprovalDecision | None:
        """返回 ALLOW/ASK/DENY；异常或 None 必须按 ASK 收敛。"""


@dataclass(frozen=True, slots=True, init=False, repr=False)
class ApprovalSecurityPolicy:
    """保存 hardline 之外的用户拒绝规则和智能审批开关。"""

    _denied_command_patterns: tuple[re.Pattern[str], ...]
    _denied_executables: frozenset[str]
    _protected_paths: tuple[str, ...]
    _hardline_protected_paths: tuple[str, ...]
    _denied_file_rules: tuple[FileDenyRule, ...]
    intelligent_approval_enabled: bool

    def __init__(
        self,
        *,
        denied_command_patterns: Sequence[str] = (),
        denied_executables: Sequence[str] = (),
        protected_paths: Sequence[str] = (),
        denied_file_rules: Sequence[Mapping] = (),
        hardline_protected_paths: Sequence[str] = (),
        intelligent_approval_enabled: bool = False,
        cwd: str | None = None,
    ) -> None:
        compiled_patterns: list[re.Pattern[str]] = []
        for index, pattern in enumerate(denied_command_patterns):
            if not isinstance(pattern, str) or not pattern.strip():
                raise ValueError(
                    "security.approval.denied_command_patterns entries must "
                    f"be non-empty strings (invalid item at index {index})"
                )
            try:
                compiled_patterns.append(re.compile(pattern, re.IGNORECASE))
            except re.error as exc:
                raise ValueError(
                    "security.approval.denied_command_patterns contains an "
                    f"invalid regex at index {index}"
                ) from exc

        normalized_executables: set[str] = set()
        for index, executable in enumerate(denied_executables):
            if not isinstance(executable, str) or not executable.strip():
                raise ValueError(
                    "security.approval.denied_executables entries must be "
                    f"non-empty strings (invalid item at index {index})"
                )
            normalized_executables.add(
                _normalize_executable_name(executable)
            )

        normalizer = PathAccessPolicy(())
        base_cwd = cwd if cwd is not None else os.getcwd()
        normalized_protected = _normalize_unique_paths(
            protected_paths,
            normalizer=normalizer,
            cwd=base_cwd,
            field_name="security.approval.protected_paths",
        )
        normalized_hardline = _normalize_unique_paths(
            hardline_protected_paths,
            normalizer=normalizer,
            cwd=base_cwd,
            field_name="hardline protected paths",
        )

        normalized_file_rules: list[FileDenyRule] = []
        for index, rule in enumerate(denied_file_rules):
            if not isinstance(rule, Mapping):
                raise ValueError(
                    "security.approval.denied_file_rules entries must be "
                    f"mappings (invalid item at index {index})"
                )
            actions = rule.get("actions")
            path_under = rule.get("path_under")
            if (
                not isinstance(actions, (list, tuple))
                or not actions
                or any(
                    not isinstance(action, str)
                    or action not in _FILE_PATH_ACTIONS
                    for action in actions
                )
            ):
                raise ValueError(
                    "security.approval.denied_file_rules actions must be a "
                    f"non-empty File action list (invalid item at index {index})"
                )
            if not isinstance(path_under, str) or not path_under.strip():
                raise ValueError(
                    "security.approval.denied_file_rules path_under must be a "
                    f"non-empty string (invalid item at index {index})"
                )
            normalized_file_rules.append(FileDenyRule(
                actions=frozenset(actions),
                path_under=normalizer.normalize_path(
                    path_under,
                    cwd=base_cwd,
                ),
            ))

        if not isinstance(intelligent_approval_enabled, bool):
            raise ValueError(
                "security.approval.intelligent_approval.enabled must be a boolean"
            )
        object.__setattr__(
            self,
            "_denied_command_patterns",
            tuple(compiled_patterns),
        )
        object.__setattr__(
            self,
            "_denied_executables",
            frozenset(normalized_executables),
        )
        object.__setattr__(self, "_protected_paths", normalized_protected)
        object.__setattr__(
            self,
            "_hardline_protected_paths",
            normalized_hardline,
        )
        object.__setattr__(
            self,
            "_denied_file_rules",
            tuple(normalized_file_rules),
        )
        object.__setattr__(
            self,
            "intelligent_approval_enabled",
            intelligent_approval_enabled,
        )

    def is_hardline_protected_path(self, path: str) -> bool:
        """供 backend 识别把安全配置映射进容器的挂载。"""
        return bool(self.hardline_paths_intersecting_mount(path))

    def hardline_paths_intersecting_mount(
        self,
        path: str,
    ) -> tuple[str, ...]:
        """返回与宿主挂载源相交的 hardline 路径，仅供 backend 内部映射。"""
        return self._paths_intersecting_mount(
            path,
            self._hardline_protected_paths,
        )

    def protected_paths_intersecting_mount(
        self,
        path: str,
    ) -> tuple[str, ...]:
        """返回与宿主挂载源相交的用户受保护路径。"""
        return self._paths_intersecting_mount(path, self._protected_paths)

    @staticmethod
    def _paths_intersecting_mount(
        path: str,
        candidates: Sequence[str],
    ) -> tuple[str, ...]:
        try:
            normalized = PathAccessPolicy(()).normalize_path(path)
        except ValueError:
            return ()
        return tuple(
            protected
            for protected in candidates
            if (
                _path_is_under(normalized, protected)
                or _path_is_under(protected, normalized)
            )
        )

    def terminal_denial(
        self,
        command: str,
        *,
        cwd: str,
        backend_context: Mapping | None = None,
    ) -> PolicyDenial | None:
        """按 hardline、用户命令规则和受保护路径顺序检查命令。"""
        context = backend_context if isinstance(backend_context, Mapping) else {}
        hardline = _hardline_terminal_denial(
            command,
            cwd=cwd,
            backend_context=context,
            hardline_protected_paths=self._hardline_protected_paths,
        )
        if hardline is not None:
            return hardline

        if any(
            pattern.search(command)
            for pattern in self._denied_command_patterns
        ):
            return PolicyDenial(
                error_type="configured_deny_rule",
                reason="命令命中用户配置的禁止命令规则",
                error="terminal command is blocked by a configured deny rule",
                decision_source="user_deny_rule",
            )

        executables = _extract_shell_executables(command)
        if any(
            _normalize_executable_name(executable)
            in self._denied_executables
            for executable in executables
        ):
            return PolicyDenial(
                error_type="configured_deny_rule",
                reason="命令引用用户配置的禁止 executable",
                error="terminal executable is blocked by a configured deny rule",
                decision_source="user_deny_rule",
            )

        if _terminal_mutates_any_path(
            command,
            cwd=cwd,
            candidate_paths=self._protected_paths,
            backend_context=context,
            container_path_field="configured_protected_paths",
        ):
            return PolicyDenial(
                error_type="configured_deny_rule",
                reason="命令尝试修改用户配置的受保护路径",
                error="terminal command targets a configured protected path",
                decision_source="user_deny_rule",
            )
        return None

    def file_denial(
        self,
        *,
        action: str,
        normalized_path: str | None,
    ) -> PolicyDenial | None:
        """检查 File hardline、受保护路径和 action/path 组合规则。"""
        if normalized_path is None:
            return None
        if action in _FILE_WRITE_ACTIONS and any(
            _path_is_under(normalized_path, protected)
            for protected in self._hardline_protected_paths
        ):
            return PolicyDenial(
                error_type="hardline_denied",
                reason="File 操作尝试修改审批配置或系统安全关键路径",
                error="file operation is blocked by a hardline safety rule",
                decision_source="hardline",
            )
        if action in _FILE_WRITE_ACTIONS and any(
            _path_is_under(normalized_path, protected)
            for protected in self._protected_paths
        ):
            return PolicyDenial(
                error_type="configured_deny_rule",
                reason="File 操作尝试修改用户配置的受保护路径",
                error="file operation targets a configured protected path",
                decision_source="user_deny_rule",
            )
        for rule in self._denied_file_rules:
            if (
                action in rule.actions
                and _path_is_under(normalized_path, rule.path_under)
            ):
                return PolicyDenial(
                    error_type="configured_deny_rule",
                    reason="File action 与目标路径命中用户拒绝组合规则",
                    error="file operation is blocked by a configured deny rule",
                    decision_source="user_deny_rule",
                )
        return None


@dataclass(frozen=True, slots=True)
class ParsedTerminalCommand:
    """结构化授权匹配使用的简单 Shell 命令表示。"""

    executable: str
    argv: tuple[str, ...]
    has_shell_operators: bool


@dataclass(frozen=True, slots=True)
class TerminalSessionGrantRule:
    """按 argv 边界匹配的 Terminal 会话授权规则。"""

    executable: str
    argv_prefix: tuple[str, ...] = ()
    cwd_policy: str = "exact"
    cwd: str | None = None
    allow_shell_operators: bool = False
    max_risk: ApprovalRiskLevel = MEDIUM


@dataclass(frozen=True, slots=True)
class FileSessionGrantRule:
    """按 action、路径范围和副作用能力匹配的 File 会话授权规则。"""

    actions: frozenset[str]
    path_under: str | None = None
    all_accessible: bool = False
    allow_sensitive: bool = False
    allow_overwrite: bool = False
    max_risk: ApprovalRiskLevel = MEDIUM


@dataclass(frozen=True, slots=True)
class TrustedApprovalGrant:
    """只能由审批处理链创建的内部授权对象。"""

    scope: str
    request_id: str
    tool_name: str
    arguments: dict
    fingerprint: str
    session_key: str
    approved_abs_path: str | None = None
    normalized_command: str | None = None
    cwd: str | None = None
    file_snapshot: dict | None = None
    session_rule: TerminalSessionGrantRule | FileSessionGrantRule | None = None
    _issuer: object = field(repr=False, compare=False, default=None)


_TRUSTED_GRANT_ISSUER = object()
_SESSION_GRANTS: dict[
    str,
    list[tuple[str, TerminalSessionGrantRule | FileSessionGrantRule]],
] = {}
_SESSION_GRANTS_LOCK = threading.Lock()
_RISK_ORDER = {
    LOW: 0,
    MEDIUM: 1,
    HIGH: 2,
    CRITICAL: 3,
}


_COMPLEX_SHELL_SYNTAX_RE = re.compile(
    r"(?:\r|\n|&&|\|\||[;&|<>`$(){}]|(?:^|\s)#)"
)
_SHELL_EXPANSION_RE = re.compile(r"[*?\[\]!]")
_SHELL_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SAFE_GIT_REVISION_RE = re.compile(r"^[A-Za-z0-9._/@:+^~-]+$")
_LS_SHORT_OPTIONS = frozenset("aAldhF1trSXingopsCxmucvqQ")
_LS_LONG_OPTIONS = frozenset({
    "--all",
    "--almost-all",
    "--directory",
    "--classify",
    "--human-readable",
    "--inode",
    "--size",
    "--numeric-uid-gid",
    "--reverse",
    "--group-directories-first",
    "--hide-control-chars",
    "--quote-name",
    "--literal",
    "--color",
    "--color=always",
    "--color=auto",
    "--color=never",
})
_LS_LONG_VALUE_OPTION_RE = re.compile(
    r"--(?:"
    r"sort=(?:none|size|time|version|extension|width)"
    r"|time=(?:atime|access|use|ctime|status|birth|creation)"
    r"|time-style=(?:full-iso|long-iso|iso|locale)"
    r"|format=(?:across|commas|horizontal|long|single-column|verbose|vertical)"
    r"|indicator-style=(?:none|slash|file-type|classify)"
    r"|width=\d+"
    r"|tabsize=\d+"
    r")"
)
_TAIL_COUNT_RE = re.compile(r"[+-]?\d+(?:[bBkKmMgGtTpPeEzZyY])?")
_GIT_STATUS_OPTIONS = frozenset({
    "--short",
    "-s",
    "--branch",
    "-b",
    "--porcelain",
    "--porcelain=v1",
    "--porcelain=v2",
    "--untracked-files=no",
    "--untracked-files=normal",
    "--untracked-files=all",
    "-uno",
    "-unormal",
    "-uall",
})
_GIT_DIFF_OPTIONS = frozenset({
    "--cached",
    "--staged",
    "--stat",
    "--shortstat",
    "--numstat",
    "--name-only",
    "--name-status",
    "--compact-summary",
    "--summary",
    "--check",
    "--quiet",
    "--exit-code",
    "--color",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
})
_GIT_LOG_OPTIONS = frozenset({
    "--oneline",
    "--decorate",
    "--decorate=short",
    "--decorate=full",
    "--no-decorate",
    "--graph",
    "--stat",
    "--shortstat",
    "--numstat",
    "--name-only",
    "--name-status",
    "--date-order",
    "--author-date-order",
    "--topo-order",
    "--reverse",
    "--all",
    "--branches",
    "--tags",
    "--remotes",
    "--first-parent",
    "--no-merges",
    "--merges",
})
_GIT_REV_PARSE_OPTIONS = frozenset({
    "--verify",
    "--quiet",
    "-q",
    "--short",
    "--abbrev-ref",
    "--symbolic-full-name",
    "--show-toplevel",
    "--show-prefix",
    "--show-cdup",
    "--show-superproject-working-tree",
    "--git-dir",
    "--git-common-dir",
    "--is-inside-work-tree",
    "--is-inside-git-dir",
    "--is-bare-repository",
    "--show-object-format",
    "--local-env-vars",
})
_RG_OPTIONS = frozenset({
    "-n",
    "--line-number",
    "-i",
    "--ignore-case",
    "-S",
    "--smart-case",
    "-F",
    "--fixed-strings",
    "-w",
    "--word-regexp",
    "-x",
    "--line-regexp",
    "-l",
    "--files-with-matches",
    "--files-without-match",
    "-c",
    "--count",
    "--count-matches",
    "--no-messages",
    "--stats",
    "--json",
    "--files",
    "--sort=path",
    "--sortr=path",
})
_FILE_WRITE_ACTIONS = frozenset({"write", "append", "replace"})
_FILE_READ_ACTIONS = frozenset({"read", "read_range"})
_FILE_METADATA_ACTIONS = frozenset({"list", "stat"})
_FILE_PATH_ACTIONS = (
    _FILE_WRITE_ACTIONS | _FILE_READ_ACTIONS | _FILE_METADATA_ACTIONS
)

# 这些模式只负责把非 hardline 的既有危险命令提升为 critical；授权判断仍由
# assess_terminal_operation 统一完成，正则编号不再代表任何 grant。
DANGEROUS_PATTERNS = [
    (
        r"rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+|.*--no-preserve-root)",
        "Recursive/force delete",
    ),
    (r"rm\s+-[a-zA-Z]*r", "Recursive delete"),
    (r"mkfs(?:\.|\s)", "Filesystem format"),
    (r"dd\s+[^\n]*\bof=", "Raw disk write"),
    (r">\s*/dev/(?:sd|nvme|mmcblk|vd|xvd)", "Direct device write"),
    (r"chmod\s+(-R\s+)?777", "World-writable permissions"),
    (r"chown\s+-R\s+", "Recursive ownership change"),
    (r"shutdown|reboot|poweroff|init\s+[06]", "System shutdown/reboot"),
    (r"kill\s+-9\s+(-1|1\b)", "Kill all processes"),
    (r":\(\)\s*\{\s*:\|\s*:\s*&\s*\}\s*;", "Fork bomb"),
    (r"DROP\s+(TABLE|DATABASE|INDEX)", "SQL destructive"),
    (r"TRUNCATE\s+TABLE", "SQL truncate"),
    (r"DELETE\s+FROM\s+\w+\s*;?\s*$", "SQL delete without WHERE"),
    (r"curl\s+.*\|\s*(bash|sh|zsh)", "Pipe to shell"),
    (r"wget\s+.*\|\s*(bash|sh|zsh)", "Pipe to shell"),
]
_COMPILED_DANGEROUS_PATTERNS = tuple(
    (re.compile(pattern, re.IGNORECASE), description)
    for pattern, description in DANGEROUS_PATTERNS
)
_SHELL_CONTROL_TOKENS = frozenset({";", "&&", "||", "|", "&", "(", ")"})
_SHELL_REDIRECTION_TOKENS = frozenset({"<", ">", ">>", "<<", "<<<", "<>"})
_SHELL_WRAPPERS = frozenset({
    "command",
    "builtin",
    "exec",
    "nohup",
    "sudo",
    "env",
    "nice",
})
_MUTATING_EXECUTABLES = frozenset({
    "rm",
    "rmdir",
    "del",
    "erase",
    "mv",
    "move",
    "cp",
    "copy",
    "install",
    "truncate",
    "touch",
    "chmod",
    "chown",
    "chgrp",
    "chattr",
    "setfacl",
    "ln",
    "tee",
    "sed",
    "perl",
    "python",
    "python3",
    "ruby",
    "node",
    "powershell",
    "powershell.exe",
    "pwsh",
    "cmd",
    "cmd.exe",
    "reg",
    "reg.exe",
    "set-content",
    "add-content",
    "clear-content",
    "out-file",
    "new-item",
    "remove-item",
    "move-item",
    "copy-item",
    "rename-item",
    "set-item",
    "set-acl",
    "icacls",
    "takeown",
    "git",
})
_RAW_DEVICE_RE = re.compile(
    r"(?i)^(?:/dev/(?:sd[a-z]\d*|nvme\d+n\d+(?:p\d+)?|mmcblk\d+(?:p\d+)?"
    r"|vd[a-z]\d*|xvd[a-z]\d*|mapper/[^/]+|dm-\d+)"
    r"|(?:\\\\\.\\|//\./)physicaldrive\d+)$"
)
_RAW_DEVICE_LITERAL_RE = re.compile(
    r"(?i)(?:\\\\\.\\|//\./)physicaldrive\d+"
)
_FORK_BOMB_RE = re.compile(
    r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;",
    re.IGNORECASE,
)
_SYSTEM_SECURITY_PATH_RE = re.compile(
    r"(?i)(?:/etc/(?:passwd|shadow|group|gshadow|sudoers(?:\.d)?|"
    r"ssh/sshd_config|pam\.d(?:/|\b)|security(?:/|\b)|systemd/system(?:/|\b))"
    r"|[A-Za-z]:[\\/]Windows[\\/]System32[\\/]config(?:[\\/]|\b))"
)
_CRITICAL_SERVICE_RE = re.compile(
    r"(?i)\b(?:systemctl\s+(?:disable|mask|stop)\b[^\n;&|]*\b"
    r"(?:sshd?|ssh|auditd|firewalld|ufw|apparmor|selinux|systemd-logind)\b"
    r"|service\s+(?:sshd?|ssh|auditd|firewalld|ufw)\s+stop\b"
    r"|(?:ufw\s+disable|iptables\s+-F\b|nft\s+flush\s+ruleset\b|"
    r"auditctl\s+-e\s*0\b|setenforce\s+0\b)"
    r"|(?:sc(?:\.exe)?|net)\s+stop\s+(?:WinDefend|MpsSvc|EventLog)\b"
    r"|Set-MpPreference\b[^\n;&|]*-DisableRealtimeMonitoring\s+\$?true\b)"
)


def _canonical_fingerprint(payload: dict) -> str:
    """对带版本的规范化操作身份生成稳定 SHA-256。"""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _identifier_fingerprint(value: str) -> str:
    """生成可展示但不可逆推出 session_key 的短摘要。"""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"sha256:{digest}"


def _safe_audit_text(value: object, *, limit: int = 240) -> str:
    """审计只保留短标签，去掉控制字符和可能携带正文的超长内容。"""
    text = str(value or "")
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text).strip()
    return text[:limit]


def emit_approval_audit(
    *,
    request_id: object,
    session_key: object,
    tool_name: object,
    risk_level: object,
    reason: object,
    decision: object,
    grant_scope: object = None,
    decision_source: object,
    timestamp: float | None = None,
) -> None:
    """输出不含参数、正文、凭证和真实 session key 的结构化审批审计。"""
    try:
        normalized_session = str(session_key or "").strip()
        record = {
            "event": "approval_decision",
            "request_id": _safe_audit_text(request_id, limit=96) or None,
            "session_security_id": (
                _identifier_fingerprint(normalized_session)
                if normalized_session
                else None
            ),
            "tool": _safe_audit_text(tool_name, limit=32),
            "risk": _safe_audit_text(risk_level, limit=16),
            "reason": _safe_audit_text(reason),
            "decision": _safe_audit_text(decision, limit=32),
            "grant_scope": (
                _safe_audit_text(grant_scope, limit=16)
                if grant_scope
                else None
            ),
            "decision_source": _safe_audit_text(
                decision_source,
                limit=32,
            ),
            "timestamp": float(
                time.time() if timestamp is None else timestamp
            ),
        }
        print(
            "  [approval:audit] "
            + json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except Exception:
        # 审计输出失败不能改变审批事务或放宽策略。
        return


def apply_intelligent_approval(
    assessment: ApprovalAssessment,
    *,
    security_policy: ApprovalSecurityPolicy,
    advisor: IntelligentApprovalAdvisor | None,
) -> ApprovalAssessment:
    """仅允许显式启用的 advisor 处理 low/medium ASK，默认保持 ASK。"""
    if (
        not security_policy.intelligent_approval_enabled
        or advisor is None
        or assessment.decision != ASK
        or assessment.risk_level in {HIGH, CRITICAL}
    ):
        return assessment
    try:
        advised = advisor.assess(assessment)
    except Exception:
        return assessment
    if advised == ALLOW:
        details = dict(assessment.details)
        details["decision_source"] = "intelligent_approval"
        return replace(
            assessment,
            decision=ALLOW,
            reason="智能审批接口批准 low/medium 操作",
            details=details,
        )
    if advised == DENY:
        details = dict(assessment.details)
        details["decision_source"] = "intelligent_approval"
        return replace(
            assessment,
            decision=DENY,
            reason="智能审批接口拒绝操作",
            error_type="intelligent_approval_denied",
            error="operation was denied by the intelligent approval advisor",
            fatal=True,
            details=details,
        )
    return assessment


def normalize_terminal_command(command: object) -> str:
    """统一换行表示，同时保留所有可能影响 Shell 语义的其它字符。"""
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a non-empty string")
    return command.replace("\r\n", "\n").replace("\r", "\n")


def normalize_approval_session_key(session_key: object) -> str:
    """拒绝缺失会话身份，避免进程级审批退化为共享默认状态。"""
    normalized = str(session_key or "").strip()
    if not normalized:
        raise ValueError("session_key must be a non-empty string")
    return normalized


def normalize_risk_level(value: object) -> ApprovalRiskLevel:
    """把持久化风险值收敛为稳定枚举。"""
    if isinstance(value, ApprovalRiskLevel):
        return value
    try:
        return ApprovalRiskLevel(str(value))
    except ValueError as exc:
        raise ValueError("invalid approval risk level") from exc


def allowed_grant_scopes(
    risk_level: ApprovalRiskLevel | str,
) -> tuple[str, ...]:
    """按风险返回用户可以选择的授权范围。"""
    risk = normalize_risk_level(risk_level)
    if risk in {LOW, MEDIUM}:
        return ("once", "session")
    if risk == HIGH:
        return ("once",)
    return ()


def is_grant_scope_allowed(
    risk_level: ApprovalRiskLevel | str,
    scope: object,
) -> bool:
    """判断 once/session 是否可用于给定风险。"""
    normalized_scope = str(scope or "").strip().lower()
    try:
        return normalized_scope in allowed_grant_scopes(risk_level)
    except ValueError:
        return False


def parse_terminal_command_for_grant(
    command: object,
) -> ParsedTerminalCommand | None:
    """仅把无动态 Shell 语义的单条命令解析成 argv。"""
    try:
        normalized = normalize_terminal_command(command).strip()
    except ValueError:
        return None
    dynamic_shell = bool(
        _COMPLEX_SHELL_SYNTAX_RE.search(normalized)
        or _SHELL_EXPANSION_RE.search(normalized)
    )
    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError:
        return None
    if (
        not tokens
        or _SHELL_ASSIGNMENT_RE.match(tokens[0])
        or any(token.startswith("~") for token in tokens)
    ):
        return None
    if dynamic_shell:
        return ParsedTerminalCommand("", (), True)
    return ParsedTerminalCommand(
        executable=tokens[0],
        argv=tuple(tokens[1:]),
        has_shell_operators=False,
    )


def _risk_at_most(
    current: ApprovalRiskLevel,
    maximum: ApprovalRiskLevel,
) -> bool:
    return _RISK_ORDER[current] <= _RISK_ORDER[maximum]


def _is_trusted_approval_grant(value: object) -> bool:
    return (
        isinstance(value, TrustedApprovalGrant)
        and value._issuer is _TRUSTED_GRANT_ISSUER
    )


def _path_is_under(path: str, parent: str) -> bool:
    """使用 commonpath 判断结构化路径范围，兼容 Windows 不同盘符。"""
    try:
        return os.path.commonpath((
            os.path.normcase(path),
            os.path.normcase(parent),
        )) == os.path.normcase(parent)
    except ValueError:
        return False


def _normalize_unique_paths(
    paths: Sequence[str],
    *,
    normalizer: PathAccessPolicy,
    cwd: str,
    field_name: str,
) -> tuple[str, ...]:
    """校验并去重需要做结构化比较的配置路径。"""
    normalized: list[str] = []
    seen: set[str] = set()
    for index, path in enumerate(paths):
        if not isinstance(path, str) or not path.strip():
            raise ValueError(
                f"{field_name} entries must be non-empty strings "
                f"(invalid item at index {index})"
            )
        value = normalizer.normalize_path(path, cwd=cwd)
        if value not in seen:
            seen.add(value)
            normalized.append(value)
    return tuple(normalized)


def _normalize_executable_name(executable: object) -> str:
    """把 executable 收敛为跨 Windows/POSIX 可比较的 basename。"""
    value = str(executable or "").strip().replace("\\", "/")
    if not value:
        return ""
    return value.rsplit("/", 1)[-1].casefold()


def _tokenize_shell(command: str) -> tuple[str, ...]:
    """尽力切分 Shell 文本；失败返回空元组，不宣称完整理解 Bash。"""
    try:
        lexer = shlex.shlex(
            command,
            posix=True,
            punctuation_chars=";&|()<>",
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        return tuple(lexer)
    except (TypeError, ValueError):
        return ()


def _shell_command_segments(command: str) -> tuple[tuple[str, ...], ...]:
    """按简单控制符拆分命令段，仅供保守 hardline 和用户规则检查。"""
    tokens = _tokenize_shell(command)
    if not tokens:
        return ()
    segments: list[tuple[str, ...]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_CONTROL_TOKENS:
            if current:
                segments.append(tuple(current))
                current = []
            continue
        current.append(token)
    if current:
        segments.append(tuple(current))
    return tuple(segments)


def _unwrap_shell_segment(
    segment: Sequence[str],
) -> tuple[str, tuple[str, ...]]:
    """去掉简单 assignment/wrapper 后返回 executable 和 argv。"""
    tokens = list(segment)
    index = 0
    while index < len(tokens) and _SHELL_ASSIGNMENT_RE.match(tokens[index]):
        index += 1
    while index < len(tokens):
        executable = _normalize_executable_name(tokens[index])
        if executable not in _SHELL_WRAPPERS:
            return tokens[index], tuple(tokens[index + 1:])
        index += 1
        while index < len(tokens):
            token = tokens[index]
            if _SHELL_ASSIGNMENT_RE.match(token):
                index += 1
                continue
            if token.startswith("-"):
                option = token
                index += 1
                if (
                    executable == "sudo"
                    and option in {"-u", "-g", "-h", "-p", "-C", "-T"}
                    and index < len(tokens)
                ):
                    index += 1
                continue
            break
    return "", ()


def _extract_shell_executables(command: str) -> tuple[str, ...]:
    """从简单或复合命令段提取 executable；解析不确定时不猜测。"""
    executables: list[str] = []
    for segment in _shell_command_segments(command):
        executable, _ = _unwrap_shell_segment(segment)
        if executable:
            executables.append(executable)
    if executables:
        return tuple(executables)
    parsed = parse_terminal_command_for_grant(command)
    if parsed is not None and parsed.executable:
        return (parsed.executable,)
    return ()


def detect_dangerous_command(
    command: str,
) -> list[tuple[int, str, str]]:
    """识别既有 critical 风险模式，不在此处创建任何授权。"""
    matches: list[tuple[int, str, str]] = []
    for index, (regex, description) in enumerate(
        _COMPILED_DANGEROUS_PATTERNS
    ):
        if regex.search(command):
            matches.append((
                index,
                DANGEROUS_PATTERNS[index][0],
                description,
            ))
    return matches


def _segment_has_mutation_intent(
    executable: str,
    argv: Sequence[str],
) -> bool:
    """保守识别会改变文件或安全状态的简单命令段。"""
    name = _normalize_executable_name(executable)
    if name not in _MUTATING_EXECUTABLES:
        return False
    lowered = [str(token).casefold() for token in argv]
    if name == "git":
        return bool(lowered) and lowered[0] in {
            "checkout",
            "restore",
            "reset",
            "clean",
            "apply",
        }
    if name == "sed":
        return any(
            token == "-i" or token.startswith("-i")
            for token in lowered
        )
    return True


def _command_has_mutation_intent(command: str) -> bool:
    """识别重定向或已知变更命令；复杂脚本仍不构成强沙箱。"""
    tokens = _tokenize_shell(command)
    if any(token in {">", ">>", "<>"} for token in tokens):
        return True
    for segment in _shell_command_segments(command):
        executable, argv = _unwrap_shell_segment(segment)
        if executable and _segment_has_mutation_intent(executable, argv):
            return True
    return False


def _candidate_shell_path_tokens(command: str) -> tuple[str, ...]:
    """提取可能作为路径使用的静态 token，不解析变量或命令替换。"""
    candidates: list[str] = []
    tokens = _tokenize_shell(command)
    skip_next = False
    for token in tokens:
        if token in _SHELL_CONTROL_TOKENS:
            skip_next = False
            continue
        if token in _SHELL_REDIRECTION_TOKENS:
            skip_next = False
            continue
        if skip_next:
            skip_next = False
            continue
        if token.startswith("-") and "=" not in token:
            continue
        value = token.split("=", 1)[1] if token.startswith("--") and "=" in token else token
        if (
            not value
            or _SHELL_ASSIGNMENT_RE.match(value)
            or any(marker in value for marker in ("$", "`", "*", "?", "[", "]"))
        ):
            continue
        candidates.append(value)
    return tuple(candidates)


def _normalize_posix_command_path(path: str, cwd: str) -> str | None:
    """规范化容器内静态路径；不展开远端用户目录或变量。"""
    if not path or path.startswith("~"):
        return None
    if path.startswith("/"):
        return posixpath.normpath(path)
    if not cwd.startswith("/"):
        return None
    return posixpath.normpath(posixpath.join(cwd, path))


def _terminal_references_any_path(
    command: str,
    *,
    cwd: str,
    candidate_paths: Sequence[str],
    backend_context: Mapping,
    container_path_field: str,
) -> bool:
    """判断命令是否显式引用本机或容器内受保护路径。"""
    tokens = _candidate_shell_path_tokens(command)
    backend_type = str(backend_context.get("backend_type", "unknown"))
    if backend_type == "local":
        normalizer = PathAccessPolicy(())
        normalized_command_text = command.replace("\\", "/").casefold()
        for token in tokens:
            try:
                normalized = normalizer.normalize_path(token, cwd=cwd)
            except ValueError:
                continue
            if any(
                _path_is_under(normalized, protected)
                for protected in candidate_paths
            ):
                return True
        # PowerShell -Command 等包装器可能把整段脚本保留为一个 token；
        # 对明确出现的绝对路径或当前 cwd 相对路径再做一次有边界的字面检查。
        for protected in candidate_paths:
            literals = {
                str(protected).replace("\\", "/").casefold(),
            }
            try:
                relative = os.path.relpath(protected, cwd)
            except ValueError:
                relative = ""
            if relative and not relative.startswith(f"..{os.sep}"):
                literals.add(relative.replace("\\", "/").casefold())
            drive, tail = os.path.splitdrive(str(protected))
            if drive and tail:
                literals.add(
                    f"/{drive[0].casefold()}/{tail.lstrip('\\/')}"
                    .replace("\\", "/")
                    .casefold()
                )
            for literal in literals:
                if not literal:
                    continue
                pattern = (
                    r"(?<![A-Za-z0-9_.-])"
                    + re.escape(literal)
                    + r"(?=$|[/\s'\";&|<>])"
                )
                if re.search(pattern, normalized_command_text):
                    return True

    container_paths = backend_context.get(container_path_field, ())
    if isinstance(container_paths, (list, tuple)):
        normalized_targets = tuple(
            posixpath.normpath(path)
            for path in container_paths
            if isinstance(path, str) and path.startswith("/")
        )
        for token in tokens:
            normalized = _normalize_posix_command_path(token, cwd)
            if normalized is not None and any(
                normalized == target
                or normalized.startswith(f"{target.rstrip('/')}/")
                for target in normalized_targets
            ):
                return True
    return False


def _terminal_mutates_any_path(
    command: str,
    *,
    cwd: str,
    candidate_paths: Sequence[str],
    backend_context: Mapping,
    container_path_field: str = "hardline_protected_paths",
) -> bool:
    return (
        _command_has_mutation_intent(command)
        and _terminal_references_any_path(
            command,
            cwd=cwd,
            candidate_paths=candidate_paths,
            backend_context=backend_context,
            container_path_field=container_path_field,
        )
    )


def _is_root_delete_target(
    target: str,
    *,
    backend_type: str,
) -> bool:
    """识别 POSIX 根、Windows 盘根及其根级通配目标。"""
    value = str(target or "").strip().replace("\\", "/")
    while value.endswith("/."):
        value = value[:-2]
    if value == "/" or value.startswith(("/*", "/.*", "/[")):
        return True
    if re.fullmatch(r"(?i)[a-z]:/(?:[*?.\[].*)?", value):
        return True
    if (
        backend_type == "local"
        and os.name == "nt"
        and re.fullmatch(r"(?i)/[a-z](?:/)?(?:[*?.\[].*)?", value)
    ):
        return True
    return False


def _hardline_root_delete(
    command: str,
    *,
    backend_type: str,
) -> bool:
    """识别递归删除系统根、磁盘根或根级通配目标。"""
    for segment in _shell_command_segments(command):
        executable, argv = _unwrap_shell_segment(segment)
        name = _normalize_executable_name(executable)
        lowered = [str(token).casefold() for token in argv]
        if name == "rm":
            recursive = any(
                token in {"-r", "-R", "--recursive"}
                or (
                    token.startswith("-")
                    and not token.startswith("--")
                    and "r" in token.casefold()
                )
                for token in argv
            )
            operands = [
                token for token in argv
                if token != "--" and not token.startswith("-")
            ]
            if recursive and any(
                _is_root_delete_target(token, backend_type=backend_type)
                for token in operands
            ):
                return True
        elif name == "find":
            operands = [token for token in argv if not token.startswith("-")]
            if "-delete" in lowered and operands and _is_root_delete_target(
                operands[0],
                backend_type=backend_type,
            ):
                return True
        elif name in {"remove-item", "rmdir", "rd", "del", "erase"}:
            recursive = any(
                token in {"-recurse", "/s"}
                for token in lowered
            )
            operands = [
                token for token in argv
                if not token.startswith(("-", "/s", "/S", "/q", "/Q"))
            ]
            if recursive and any(
                _is_root_delete_target(token, backend_type=backend_type)
                for token in operands
            ):
                return True
    # Windows 路径在外层 Bash 中可能被当成转义字符；对明显的根目录删除
    # 再按原始命令段保守复检，覆盖 PowerShell/cmd 包装调用。
    for raw_segment in re.split(r"[;&|\r\n]+", command):
        if not re.search(
            r"(?i)\b(?:rm|remove-item|rmdir|rd|del|erase)\b",
            raw_segment,
        ):
            continue
        if not re.search(
            r"(?i)(?:--recursive\b|-{1,2}[A-Za-z]*r[A-Za-z]*\b|"
            r"-recurse\b|/s\b)",
            raw_segment,
        ):
            continue
        drive_root = re.search(
            r"(?i)(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/])"
            r"(?=$|[\s'\"<>*?.\[])"
            ,
            raw_segment,
        )
        git_bash_root = (
            backend_type == "local"
            and os.name == "nt"
            and re.search(
                r"(?i)(?<![A-Za-z0-9])/[A-Za-z]/?"
                r"(?=$|[\s'\"<>*?.\[])",
                raw_segment,
            )
        )
        if drive_root or git_bash_root:
            return True
    return False


def _hardline_raw_device_write(command: str) -> bool:
    """识别 dd/tee/重定向等明显裸磁盘或块设备写入。"""
    if _RAW_DEVICE_LITERAL_RE.search(command):
        return True
    tokens = _tokenize_shell(command)
    for index, token in enumerate(tokens):
        value = token
        if token.casefold().startswith("of="):
            value = token.split("=", 1)[1]
        elif token in {">", ">>", "<>"} and index + 1 < len(tokens):
            value = tokens[index + 1]
        if _RAW_DEVICE_RE.fullmatch(value.replace("\\", "/")):
            return True
    for segment in _shell_command_segments(command):
        executable, argv = _unwrap_shell_segment(segment)
        if _normalize_executable_name(executable) == "tee" and any(
            _RAW_DEVICE_RE.fullmatch(str(token).replace("\\", "/"))
            for token in argv
        ):
            return True
    return False


def _hardline_terminal_denial(
    command: str,
    *,
    cwd: str,
    backend_context: Mapping,
    hardline_protected_paths: Sequence[str],
) -> PolicyDenial | None:
    """返回任何 grant 都不能覆盖的 Terminal hardline 拒绝。"""
    backend_type = str(backend_context.get("backend_type", "unknown"))
    executables = {
        _normalize_executable_name(executable)
        for executable in _extract_shell_executables(command)
    }
    if _hardline_root_delete(command, backend_type=backend_type):
        reason = "命令尝试递归删除系统根目录、磁盘根目录或根级通配目标"
    elif any(
        executable.startswith("mkfs")
        or executable in {"mke2fs", "newfs"}
        for executable in executables
    ):
        reason = "命令尝试格式化文件系统"
    elif _hardline_raw_device_write(command):
        reason = "命令尝试写入裸磁盘或块设备"
    elif _FORK_BOMB_RE.search(command):
        reason = "命令包含 fork bomb"
    elif _CRITICAL_SERVICE_RE.search(command):
        reason = "命令尝试破坏系统关键服务或安全控制"
    elif (
        _SYSTEM_SECURITY_PATH_RE.search(command)
        and _command_has_mutation_intent(command)
    ):
        reason = "命令尝试修改系统安全关键配置"
    elif _terminal_mutates_any_path(
        command,
        cwd=cwd,
        candidate_paths=hardline_protected_paths,
        backend_context=backend_context,
    ):
        reason = "命令尝试修改审批配置以关闭安全机制"
    else:
        return None
    return PolicyDenial(
        error_type="hardline_denied",
        reason=reason,
        error="terminal command is blocked by a hardline safety rule",
        decision_source="hardline",
    )


def assess_backend_risk(
    backend_context: Mapping | None,
) -> BackendRiskAssessment:
    """区分本机、普通容器、宿主挂载、Docker socket 与 SSH。"""
    context = backend_context if isinstance(backend_context, Mapping) else {}
    backend_type = str(context.get("backend_type", "unknown"))
    if backend_type == "docker_socket":
        return BackendRiskAssessment(
            backend_type=backend_type,
            risk_floor=CRITICAL,
            automatic_allowance=False,
            reason="Docker socket 提供近似宿主机控制能力",
        )
    if backend_type == "docker_host_mount":
        return BackendRiskAssessment(
            backend_type=backend_type,
            risk_floor=HIGH,
            automatic_allowance=False,
            reason="Docker backend 暴露宿主机挂载",
        )
    if backend_type == "ssh":
        return BackendRiskAssessment(
            backend_type=backend_type,
            risk_floor=HIGH,
            automatic_allowance=False,
            reason="SSH backend 在独立远端主机执行",
        )
    if backend_type in {"local", "docker"}:
        return BackendRiskAssessment(
            backend_type=backend_type,
            risk_floor=LOW,
            automatic_allowance=True,
            reason=(
                "本机 backend 按命令自身风险判定"
                if backend_type == "local"
                else "无宿主挂载的 Docker 仍按命令自身风险判定"
            ),
        )
    return BackendRiskAssessment(
        backend_type=backend_type or "unknown",
        risk_floor=HIGH,
        automatic_allowance=False,
        reason="未知 backend 不获得自动免审",
    )


def _backend_fingerprint_payload(backend_context: Mapping | None) -> dict:
    """只把非敏感 backend 风险标志写入审批指纹和详情。"""
    context = backend_context if isinstance(backend_context, Mapping) else {}
    return {
        "backend_type": str(context.get("backend_type", "unknown")),
        "host_mounts": bool(context.get("host_mounts", False)),
        "docker_socket": bool(context.get("docker_socket", False)),
        "remote_host": bool(context.get("remote_host", False)),
    }


def _max_risk(
    first: ApprovalRiskLevel,
    second: ApprovalRiskLevel,
) -> ApprovalRiskLevel:
    return first if _RISK_ORDER[first] >= _RISK_ORDER[second] else second


DEFAULT_APPROVAL_SECURITY_POLICY = ApprovalSecurityPolicy()


def _file_snapshot_required(arguments: dict) -> bool:
    """判断审批身份是否必须绑定执行前文件状态。"""
    action = arguments.get("action")
    return (
        action in {"replace", "append"}
        or (
            action == "write"
            and bool(arguments.get("overwrite", False))
        )
    )


def _file_operation_mutates_existing(
    arguments: dict,
    file_snapshot: dict | None,
) -> bool:
    """把覆盖写、替换和已有文件追加统一收敛为覆盖能力。"""
    if not file_snapshot or file_snapshot.get("exists") is not True:
        return False
    action = arguments.get("action")
    return (
        action in {"replace", "append"}
        or (
            action == "write"
            and bool(arguments.get("overwrite", False))
        )
    )


def issue_trusted_approval_grant(
    request: dict,
    *,
    scope: str,
) -> TrustedApprovalGrant:
    """从数据库已 claim 请求创建不可由模型参数伪造的内部 grant。"""
    normalized_scope = str(scope or "").strip().lower()
    tool_name = str(request.get("tool_name", ""))
    arguments = request.get("tool_args")
    details = request.get("details")
    session_key = normalize_approval_session_key(
        request.get("conversation_id")
    )
    if tool_name not in {"file", "terminal", "gateway_send_file", "cron"}:
        raise ValueError("unsupported approval grant tool")
    if not isinstance(arguments, dict) or not isinstance(details, dict):
        raise ValueError("approval grant request is invalid")
    risk_level = normalize_risk_level(details.get("risk_level"))
    request_session_key = normalize_approval_session_key(
        request.get("session_key")
    )
    if request_session_key != session_key:
        raise ValueError("approval grant session binding is invalid")
    if str(request.get("tool_call_id", "")).strip() == "":
        raise ValueError("approval grant tool call binding is invalid")
    if request.get("status") != "executing":
        raise ValueError("approval grant request has not been claimed")
    if str(request.get("grant_scope", "")).strip().lower() != normalized_scope:
        raise ValueError("approval grant scope binding is invalid")
    try:
        created_at = float(request.get("created_at"))
        expires_at = float(request.get("expires_at"))
        claimed_at = float(request.get("updated_at"))
    except (TypeError, ValueError) as exc:
        raise ValueError("approval grant lifetime binding is invalid") from exc
    if (
        not (created_at <= claimed_at < expires_at)
    ):
        raise ValueError("approval grant request has expired")
    if not is_grant_scope_allowed(risk_level, normalized_scope):
        raise ValueError("approval grant scope is not allowed for this risk")
    fingerprint = details.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint.startswith(
        "sha256:"
    ):
        raise ValueError("approval grant fingerprint is invalid")
    if request.get("fingerprint") != fingerprint:
        raise ValueError("approval grant fingerprint binding is invalid")

    request_id = str(request.get("id", ""))
    if not request_id.startswith("approval_"):
        raise ValueError("approval grant request id is invalid")

    if tool_name == "terminal":
        normalized_command = normalize_terminal_command(
            details.get("normalized_command")
        )
        cwd = str(details.get("cwd", "") or "").strip()
        if not cwd:
            raise ValueError("approval grant cwd is invalid")
        session_rule = None
        if normalized_scope == "session":
            parsed = parse_terminal_command_for_grant(normalized_command)
            if (
                parsed is None
                or parsed.has_shell_operators
                or not parsed.executable
            ):
                raise ValueError(
                    "terminal command cannot create a session grant"
                )
            session_rule = TerminalSessionGrantRule(
                executable=parsed.executable,
                argv_prefix=parsed.argv,
                cwd_policy="exact",
                cwd=cwd,
                allow_shell_operators=False,
                max_risk=risk_level,
            )
        return TrustedApprovalGrant(
            scope=normalized_scope,
            request_id=request_id,
            tool_name=tool_name,
            arguments=dict(arguments),
            fingerprint=fingerprint,
            session_key=session_key,
            normalized_command=normalized_command,
            cwd=cwd,
            session_rule=session_rule,
            _issuer=_TRUSTED_GRANT_ISSUER,
        )

    if tool_name == "cron":
        if normalized_scope != "once":
            raise ValueError("cron capability approval only supports once scope")
        return TrustedApprovalGrant(
            scope=normalized_scope,
            request_id=request_id,
            tool_name=tool_name,
            arguments=dict(arguments),
            fingerprint=fingerprint,
            session_key=session_key,
            _issuer=_TRUSTED_GRANT_ISSUER,
        )

    if tool_name == "gateway_send_file":
        if normalized_scope != "once":
            raise ValueError(
                "gateway_send_file approval only supports once scope"
            )
        file_snapshot = _normalize_gateway_send_file_snapshot(
            details.get("file_snapshot")
        )
        approved_abs_path = details.get("abs_path")
        if approved_abs_path != file_snapshot["abs_path"]:
            raise ValueError("approval grant file path is invalid")
        return TrustedApprovalGrant(
            scope=normalized_scope,
            request_id=request_id,
            tool_name=tool_name,
            arguments=dict(arguments),
            fingerprint=fingerprint,
            session_key=session_key,
            approved_abs_path=approved_abs_path,
            file_snapshot=file_snapshot,
            _issuer=_TRUSTED_GRANT_ISSUER,
        )

    approved_abs_path = details.get("abs_path")
    if not isinstance(approved_abs_path, str) or not approved_abs_path:
        raise ValueError("approval grant file path is invalid")
    file_snapshot = details.get("file_snapshot")
    if file_snapshot is not None:
        file_snapshot = normalize_file_state_snapshot(file_snapshot)
    if _file_snapshot_required(arguments) and file_snapshot is None:
        raise ValueError("approval grant file snapshot is required")
    session_rule = None
    if normalized_scope == "session":
        action = str(arguments.get("action", ""))
        all_accessible = action in (
            _FILE_READ_ACTIONS | _FILE_METADATA_ACTIONS
        )
        path_under = (
            None
            if all_accessible
            else os.path.dirname(approved_abs_path)
        )
        session_rule = FileSessionGrantRule(
            actions=frozenset({action}),
            path_under=path_under,
            all_accessible=all_accessible,
            allow_sensitive=False,
            allow_overwrite=_file_operation_mutates_existing(
                arguments,
                file_snapshot,
            ),
            max_risk=risk_level,
        )
    return TrustedApprovalGrant(
        scope=normalized_scope,
        request_id=request_id,
        tool_name=tool_name,
        arguments=dict(arguments),
        fingerprint=fingerprint,
        session_key=session_key,
        approved_abs_path=approved_abs_path,
        file_snapshot=file_snapshot,
        session_rule=session_rule,
        _issuer=_TRUSTED_GRANT_ISSUER,
    )


def activate_session_grant(grant: TrustedApprovalGrant) -> bool:
    """只登记可信 session grant；once grant 不进入共享状态。"""
    if (
        not _is_trusted_approval_grant(grant)
        or grant.scope != "session"
        or grant.session_rule is None
    ):
        return False
    with _SESSION_GRANTS_LOCK:
        entries = _SESSION_GRANTS.setdefault(grant.session_key, [])
        item = (grant.request_id, grant.session_rule)
        if item not in entries:
            entries.append(item)
    return True


def clear_session_grants(session_key: str) -> None:
    """session/backend 生命周期结束时删除全部结构化授权。"""
    normalized = str(session_key or "").strip()
    if not normalized:
        return
    with _SESSION_GRANTS_LOCK:
        _SESSION_GRANTS.pop(normalized, None)


def _session_grant_rules(
    session_key: str,
) -> tuple[TerminalSessionGrantRule | FileSessionGrantRule, ...]:
    normalized = normalize_approval_session_key(session_key)
    with _SESSION_GRANTS_LOCK:
        return tuple(rule for _, rule in _SESSION_GRANTS.get(normalized, ()))


def terminal_session_grant_matches(
    *,
    session_key: str,
    command: str,
    cwd: str,
    risk_level: ApprovalRiskLevel,
) -> bool:
    """重新解析命令并按 executable/argv/cwd/risk 匹配会话规则。"""
    parsed = parse_terminal_command_for_grant(command)
    if parsed is None:
        return False
    for rule in _session_grant_rules(session_key):
        if not isinstance(rule, TerminalSessionGrantRule):
            continue
        if parsed.has_shell_operators and not rule.allow_shell_operators:
            continue
        if not parsed.executable or parsed.executable != rule.executable:
            continue
        if parsed.argv[:len(rule.argv_prefix)] != rule.argv_prefix:
            continue
        if rule.cwd_policy == "exact":
            if not rule.cwd or cwd != rule.cwd:
                continue
        elif rule.cwd_policy != "any":
            continue
        if _risk_at_most(risk_level, rule.max_risk):
            return True
    return False


def file_session_grant_matches(
    *,
    session_key: str,
    action: str,
    normalized_path: str | None,
    sensitive: bool,
    overwrite: bool,
    risk_level: ApprovalRiskLevel,
) -> bool:
    """按 action、结构化路径范围和副作用能力匹配 File 会话规则。"""
    for rule in _session_grant_rules(session_key):
        if not isinstance(rule, FileSessionGrantRule):
            continue
        if action not in rule.actions:
            continue
        if sensitive and not rule.allow_sensitive:
            continue
        if overwrite and not rule.allow_overwrite:
            continue
        if not _risk_at_most(risk_level, rule.max_risk):
            continue
        if rule.all_accessible:
            if action not in (_FILE_READ_ACTIONS | _FILE_METADATA_ACTIONS):
                continue
        elif (
            normalized_path is None
            or not rule.path_under
            or not _path_is_under(normalized_path, rule.path_under)
        ):
            continue
        return True
    return False


def _terminal_classification(
    automatically_allowed: bool,
    operation_type: str,
    risk_level: ApprovalRiskLevel,
    reason: str,
    target_paths: Sequence[str] = (),
) -> TerminalCommandClassification:
    """构造不可变分类结果，并冻结静态目标路径。"""
    return TerminalCommandClassification(
        automatically_allowed=automatically_allowed,
        operation_type=operation_type,
        risk_level=risk_level,
        reason=reason,
        target_paths=tuple(target_paths),
    )


def _is_static_path_token(token: str) -> bool:
    """只接受不依赖 Shell 展开、glob 或隐式目录变量的路径 token。"""
    return bool(
        token
        and token not in {"-", "--"}
        and not token.startswith("~")
        and not _SHELL_EXPANSION_RE.search(token)
        and "\x00" not in token
    )


_SAFE_STDERR_NULL_RE = re.compile(
    r"(?i)(?<!\S)2\s*>>?\s*/dev/null(?=$|\s|[;&|])"
)
_FIND_MUTATING_PRIMARIES = frozenset({
    "-delete",
    "-exec",
    "-execdir",
    "-ok",
    "-okdir",
    "-fls",
    "-fprint",
    "-fprint0",
    "-fprintf",
})


def _unquoted_shell_view(command: str) -> str:
    """保留未引用字符的位置，隐藏引号内容和反斜杠转义字符。"""
    view = list(command)
    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote is not None:
            view[index] = " "
            if char == "\\" and quote == '"' and index + 1 < len(command):
                index += 1
                view[index] = " "
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            view[index] = " "
            index += 1
            continue
        if char == "\\" and index + 1 < len(command):
            view[index] = " "
            index += 1
            view[index] = " "
        index += 1
    return "".join(view)


def _strip_safe_stderr_null_redirects(command: str) -> str:
    """移除明确只丢弃 stderr 的重定向，保留其余 Shell 语义位置。"""
    view = _unquoted_shell_view(command)
    matches = tuple(_SAFE_STDERR_NULL_RE.finditer(view))
    if not matches:
        return command
    stripped = list(command)
    for match in matches:
        stripped[match.start():match.end()] = " " * (
            match.end() - match.start()
        )
    return "".join(stripped)


def _split_static_shell_segments(
    command: str,
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """按引号感知方式拆分简单 Shell；动态语义或写重定向返回 None。"""
    command = _strip_safe_stderr_null_redirects(command)
    segments: list[str] = []
    operators: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    index = 0

    def flush_segment() -> bool:
        segment = "".join(buffer).strip()
        buffer.clear()
        if not segment:
            return False
        segments.append(segment)
        return True

    while index < len(command):
        char = command[index]
        if quote == "'":
            buffer.append(char)
            if char == "'":
                quote = None
            index += 1
            continue
        if quote == '"':
            buffer.append(char)
            if char == "\\" and index + 1 < len(command):
                index += 1
                buffer.append(command[index])
            elif char == '"':
                quote = None
            elif char in {"$", "`"}:
                return None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            buffer.append(char)
            index += 1
            continue
        if char == "\\" and index + 1 < len(command):
            buffer.extend((char, command[index + 1]))
            index += 2
            continue
        if char in "\r\n`$(){}<>":
            return None
        if char in "*?[]!":
            return None
        if char == "#" and (
            not buffer or str(buffer[-1]).isspace()
        ):
            return None
        if char == "&":
            if index + 1 >= len(command) or command[index + 1] != "&":
                return None
            if not flush_segment():
                return None
            operators.append("&&")
            index += 2
            continue
        if char == "|":
            operator = "||" if (
                index + 1 < len(command) and command[index + 1] == "|"
            ) else "|"
            if not flush_segment():
                return None
            operators.append(operator)
            index += len(operator)
            continue
        if char == ";":
            if not flush_segment():
                return None
            operators.append(";")
            index += 1
            continue
        buffer.append(char)
        index += 1

    if quote is not None or not flush_segment():
        return None
    if len(segments) != len(operators) + 1:
        return None
    return tuple(segments), tuple(operators)


def _classify_ls(tokens: Sequence[str]) -> TerminalCommandClassification:
    """识别不会写入、执行外部 helper 或依赖 Shell 展开的 ls 形式。"""
    target_paths: list[str] = []
    options_ended = False
    for token in tokens:
        if not options_ended and token == "--":
            options_ended = True
            continue
        if not options_ended and token.startswith("-") and token != "-":
            if token.startswith("--"):
                if (
                    token not in _LS_LONG_OPTIONS
                    and not _LS_LONG_VALUE_OPTION_RE.fullmatch(token)
                ):
                    return _terminal_classification(
                        False,
                        "terminal.ls",
                        MEDIUM,
                        "ls 包含未列入安全白名单的选项",
                    )
                continue
            if not token[1:] or any(
                char not in _LS_SHORT_OPTIONS for char in token[1:]
            ):
                return _terminal_classification(
                    False,
                    "terminal.ls",
                    MEDIUM,
                    "ls 包含未列入安全白名单的选项",
                )
            continue
        if not _is_static_path_token(token):
            return _terminal_classification(
                False,
                "terminal.ls",
                HIGH,
                "ls 的目标路径需要 Shell 动态展开",
            )
        target_paths.append(token)
    return _terminal_classification(
        True,
        "terminal.ls",
        LOW,
        "命令命中简单只读 ls 白名单",
        target_paths,
    )


def _classify_static_query(
    executable: str,
    tokens: Sequence[str],
    *,
    short_options: str = "",
    long_options: Sequence[str] = (),
    require_operands: bool = False,
    allow_operands: bool = True,
    target_paths: bool = False,
    risk_level: ApprovalRiskLevel = LOW,
) -> TerminalCommandClassification:
    """识别只接受固定选项和静态参数的常用查询命令。"""
    operands: list[str] = []
    options_ended = False
    allowed_short = frozenset(short_options)
    allowed_long = frozenset(long_options)
    for token in tokens:
        if not options_ended and token == "--":
            options_ended = True
            continue
        if not options_ended and token.startswith("-") and token != "-":
            if token.startswith("--"):
                option_allowed = token in allowed_long
            else:
                option_allowed = bool(token[1:]) and all(
                    char in allowed_short for char in token[1:]
                )
            if not option_allowed:
                return _terminal_classification(
                    False,
                    f"terminal.{executable}",
                    MEDIUM,
                    f"{executable} 包含未列入只读白名单的选项",
                )
            continue
        if not allow_operands:
            return _terminal_classification(
                False,
                f"terminal.{executable}",
                MEDIUM,
                f"{executable} 包含不支持的额外参数",
            )
        if not _is_static_path_token(token):
            return _terminal_classification(
                False,
                f"terminal.{executable}",
                HIGH,
                f"{executable} 的参数需要 Shell 动态展开",
            )
        operands.append(token)
    if require_operands and not operands:
        return _terminal_classification(
            False,
            f"terminal.{executable}",
            MEDIUM,
            f"{executable} 缺少可静态识别的目标",
        )
    return _terminal_classification(
        True,
        f"terminal.{executable}",
        risk_level,
        f"命令命中只读 {executable} 白名单",
        operands if target_paths else (),
    )


def _classify_tail(tokens: Sequence[str]) -> TerminalCommandClassification:
    """允许有限行数/字节读取，拒绝 follow 和进程等待模式。"""
    target_paths: list[str] = []
    options_ended = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not options_ended and token == "--":
            options_ended = True
            index += 1
            continue
        if not options_ended and token.startswith("-") and token != "-":
            if token in {
                "-q", "--quiet", "--silent",
                "-v", "--verbose",
                "-z", "--zero-terminated",
            }:
                index += 1
                continue
            if token in {"-n", "--lines", "-c", "--bytes"}:
                if (
                    index + 1 >= len(tokens)
                    or not _TAIL_COUNT_RE.fullmatch(tokens[index + 1])
                ):
                    return _terminal_classification(
                        False,
                        "terminal.tail",
                        MEDIUM,
                        "tail 的读取范围参数无法安全解析",
                    )
                index += 2
                continue
            if (
                re.fullmatch(
                    r"-(?:n|c)[+-]?\d+(?:[bBkKmMgGtTpPeEzZyY])?",
                    token,
                )
                or re.fullmatch(
                    r"--(?:lines|bytes)=[+-]?\d+(?:[bBkKmMgGtTpPeEzZyY])?",
                    token,
                )
                or re.fullmatch(r"-\d+", token)
            ):
                index += 1
                continue
            return _terminal_classification(
                False,
                "terminal.tail",
                MEDIUM,
                "tail 包含 follow、进程等待或未列入白名单的选项",
            )
        if not _is_static_path_token(token):
            return _terminal_classification(
                False,
                "terminal.tail",
                HIGH,
                "tail 的目标路径需要 Shell 动态展开",
            )
        target_paths.append(token)
        index += 1
    return _terminal_classification(
        True,
        "terminal.tail",
        MEDIUM,
        "命令命中有限读取的 tail 白名单",
        target_paths,
    )


def _classify_stat(tokens: Sequence[str]) -> TerminalCommandClassification:
    """允许读取文件或文件系统元数据，不开放未知 stat 扩展选项。"""
    target_paths: list[str] = []
    options_ended = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not options_ended and token == "--":
            options_ended = True
            index += 1
            continue
        if not options_ended and token.startswith("-") and token != "-":
            if token in {
                "-L", "--dereference", "-f", "--file-system",
                "-t", "--terse",
            }:
                index += 1
                continue
            if token in {"-c", "--format", "--printf"}:
                if index + 1 >= len(tokens):
                    return _terminal_classification(
                        False,
                        "terminal.stat",
                        MEDIUM,
                        "stat 的输出格式参数缺少值",
                    )
                index += 2
                continue
            if (
                (token.startswith("-c") and len(token) > 2)
                or (token.startswith("--format=") and len(token) > 9)
                or (token.startswith("--printf=") and len(token) > 9)
            ):
                index += 1
                continue
            return _terminal_classification(
                False,
                "terminal.stat",
                MEDIUM,
                "stat 包含未列入只读白名单的选项",
            )
        if not _is_static_path_token(token):
            return _terminal_classification(
                False,
                "terminal.stat",
                HIGH,
                "stat 的目标路径需要 Shell 动态展开",
            )
        target_paths.append(token)
        index += 1
    if not target_paths:
        return _terminal_classification(
            False,
            "terminal.stat",
            MEDIUM,
            "stat 缺少可静态识别的目标路径",
        )
    return _terminal_classification(
        True,
        "terminal.stat",
        MEDIUM,
        "命令命中只读文件元数据 stat 白名单",
        target_paths,
    )


def _classify_find(tokens: Sequence[str]) -> TerminalCommandClassification:
    """允许不含执行、删除或文件输出 primary 的只读 find。"""
    lowered = [str(token).casefold() for token in tokens]
    if any(
        token.split("=", 1)[0] in _FIND_MUTATING_PRIMARIES
        for token in lowered
    ):
        return _terminal_classification(
            False,
            "terminal.find",
            HIGH,
            "find 包含删除、执行外部命令或写文件操作",
        )

    target_paths: list[str] = []
    expression_started = False
    for token in tokens:
        if not expression_started and token.startswith("-"):
            expression_started = True
            continue
        if expression_started:
            continue
        if not _is_static_path_token(token):
            return _terminal_classification(
                False,
                "terminal.find",
                HIGH,
                "find 的搜索根路径需要 Shell 动态展开",
            )
        target_paths.append(token)
    if not target_paths:
        target_paths.append(".")
    return _terminal_classification(
        True,
        "terminal.find",
        MEDIUM,
        "命令命中不执行、不删除且不写文件的只读 find 白名单",
        target_paths,
    )


def _classify_du(tokens: Sequence[str]) -> TerminalCommandClassification:
    """识别只统计磁盘占用的 du；du 本身没有写入或 helper 执行能力。"""
    target_paths = [
        token for token in tokens
        if token == "-" or not token.startswith("-")
    ]
    if any(not _is_static_path_token(path) for path in target_paths):
        return _terminal_classification(
            False,
            "terminal.du",
            HIGH,
            "du 的目标路径需要 Shell 动态展开",
        )
    return _terminal_classification(
        True,
        "terminal.du",
        MEDIUM,
        "命令命中只读磁盘占用统计白名单",
        target_paths or (".",),
    )


def _classify_readonly_filter(
    executable: str,
    tokens: Sequence[str],
) -> TerminalCommandClassification:
    """允许只从参数、文件或 stdin 读取并向 stdout 输出的过滤命令。"""
    return _terminal_classification(
        True,
        f"terminal.{executable}",
        MEDIUM,
        f"命令命中只读 {executable} 白名单",
    )


def _classify_git_status(
    tokens: Sequence[str],
) -> TerminalCommandClassification:
    """识别只读取仓库状态的 git status 形式。"""
    target_paths: list[str] = []
    pathspec_mode = False
    for token in tokens:
        if not pathspec_mode and token == "--":
            pathspec_mode = True
            continue
        if not pathspec_mode:
            if (
                token not in _GIT_STATUS_OPTIONS
                and not re.fullmatch(r"-[sb]+", token)
            ):
                return _terminal_classification(
                    False,
                    "terminal.git_status",
                    MEDIUM,
                    "git status 包含未列入只读白名单的参数",
                )
            continue
        if not _is_static_path_token(token):
            return _terminal_classification(
                False,
                "terminal.git_status",
                HIGH,
                "git status 的 pathspec 需要 Shell 动态展开",
            )
        target_paths.append(token)
    return _terminal_classification(
        True,
        "terminal.git_status",
        LOW,
        "命令命中只读 git status 白名单",
        target_paths,
    )


def _is_safe_git_revision(token: str) -> bool:
    """限定无需 Shell 展开的常见 revision/ref 表达式。"""
    return bool(token and _SAFE_GIT_REVISION_RE.fullmatch(token))


def _classify_git_diff(
    tokens: Sequence[str],
) -> TerminalCommandClassification:
    """识别未显式写输出或启用外部 helper 的 git diff 形式。"""
    target_paths: list[str] = []
    pathspec_mode = False
    for token in tokens:
        if not pathspec_mode and token == "--":
            pathspec_mode = True
            continue
        if pathspec_mode:
            if not _is_static_path_token(token):
                return _terminal_classification(
                    False,
                    "terminal.git_diff",
                    HIGH,
                    "git diff 的目标路径需要 Shell 动态展开",
                )
            target_paths.append(token)
            continue
        if (
            token in _GIT_DIFF_OPTIONS
            or re.fullmatch(r"(?:-U\d+|--unified=\d+)", token)
            or re.fullmatch(r"--inter-hunk-context=\d+", token)
        ):
            continue
        if token.startswith("-") or not _is_safe_git_revision(token):
            return _terminal_classification(
                False,
                "terminal.git_diff",
                HIGH,
                "git diff 包含无法确认只读语义的参数",
            )
    return _terminal_classification(
        True,
        "terminal.git_diff",
        MEDIUM,
        "命令命中只读 git diff 白名单",
        target_paths,
    )


def _classify_git_log(
    tokens: Sequence[str],
) -> TerminalCommandClassification:
    """识别只输出提交历史且不依赖复杂 Shell 语法的 git log。"""
    target_paths: list[str] = []
    pathspec_mode = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not pathspec_mode and token == "--":
            pathspec_mode = True
            index += 1
            continue
        if pathspec_mode:
            if not _is_static_path_token(token):
                return _terminal_classification(
                    False,
                    "terminal.git_log",
                    MEDIUM,
                    "git log 的目标路径需要 Shell 动态展开",
                )
            target_paths.append(token)
            index += 1
            continue
        if token in {"-n", "--max-count", "--skip"}:
            if index + 1 >= len(tokens) or not tokens[index + 1].isdigit():
                return _terminal_classification(
                    False,
                    "terminal.git_log",
                    HIGH,
                    "git log 的 -n 参数无法安全解析",
                )
            index += 2
            continue
        if (
            token in _GIT_LOG_OPTIONS
            or re.fullmatch(r"-n\d+", token)
            or re.fullmatch(r"-\d+", token)
            or re.fullmatch(r"--(?:max-count|skip)=\d+", token)
            or re.fullmatch(
                r"--(?:since|until|author|grep|format|pretty|date)=.+",
                token,
            )
        ):
            index += 1
            continue
        if token.startswith("-") or not _is_safe_git_revision(token):
            return _terminal_classification(
                False,
                "terminal.git_log",
                MEDIUM,
                "git log 包含无法确认只读语义的参数",
            )
        index += 1
    return _terminal_classification(
        True,
        "terminal.git_log",
        MEDIUM,
        "命令命中只读 git log 白名单",
        target_paths,
    )


def _classify_git_rev_parse(
    tokens: Sequence[str],
) -> TerminalCommandClassification:
    """识别仅解析仓库或 revision 信息的 git rev-parse。"""
    target_paths: list[str] = []
    path_mode = False
    for token in tokens:
        if not path_mode and token == "--":
            path_mode = True
            continue
        if path_mode:
            if not _is_static_path_token(token):
                return _terminal_classification(
                    False,
                    "terminal.git_rev_parse",
                    MEDIUM,
                    "git rev-parse 的目标参数需要 Shell 动态展开",
                )
            target_paths.append(token)
            continue
        if (
            token in _GIT_REV_PARSE_OPTIONS
            or re.fullmatch(r"--short=\d+", token)
            or re.fullmatch(r"--abbrev-ref=(?:strict|loose)", token)
            or re.fullmatch(r"--path-format=(?:absolute|relative)", token)
            or re.fullmatch(
                r"--show-object-format=(?:storage|input|output)",
                token,
            )
        ):
            continue
        if token.startswith("-") or not _is_safe_git_revision(token):
            return _terminal_classification(
                False,
                "terminal.git_rev_parse",
                MEDIUM,
                "git rev-parse 包含无法确认只读语义的参数",
            )
    return _terminal_classification(
        True,
        "terminal.git_rev_parse",
        LOW,
        "命令命中只读 git rev-parse 白名单",
        target_paths,
    )


def _classify_git(tokens: Sequence[str]) -> TerminalCommandClassification:
    """按明确子命令分派 Git 只读白名单。"""
    if not tokens:
        return _terminal_classification(
            False,
            "terminal.git",
            HIGH,
            "git 缺少可识别的只读子命令",
        )
    subcommand, *arguments = tokens
    if subcommand == "status":
        return _classify_git_status(arguments)
    if subcommand == "diff":
        return _classify_git_diff(arguments)
    if subcommand == "log":
        return _classify_git_log(arguments)
    if subcommand == "branch" and arguments == ["--show-current"]:
        return _terminal_classification(
            True,
            "terminal.git_branch_show_current",
            LOW,
            "命令命中 git branch --show-current 白名单",
        )
    if subcommand == "rev-parse":
        return _classify_git_rev_parse(arguments)
    return _terminal_classification(
        False,
        f"terminal.git_{subcommand.replace('-', '_')}",
        HIGH,
        "git 子命令不在只读白名单中",
    )


def _classify_rg(tokens: Sequence[str]) -> TerminalCommandClassification:
    """识别不启用预处理器、隐藏文件遍历或复杂 glob 的 rg。"""
    positionals: list[str] = []
    options_ended = False
    files_mode = False
    for token in tokens:
        if not options_ended and token == "--":
            options_ended = True
            continue
        if not options_ended and token.startswith("-") and token != "-":
            if token in _RG_OPTIONS:
                files_mode = files_mode or token == "--files"
                continue
            if (
                re.fullmatch(r"-[niSFlcwx]+", token)
                or re.fullmatch(r"-[CABm]\d+", token)
                or re.fullmatch(
                    r"--(?:max-count|max-depth|context|before-context|after-context)=\d+",
                    token,
                )
            ):
                continue
            return _terminal_classification(
                False,
                "terminal.rg",
                (
                    HIGH
                    if token.startswith("--pre")
                    else MEDIUM
                ),
                "rg 包含未列入安全白名单的选项",
            )
        positionals.append(token)

    if files_mode:
        target_paths = positionals
    else:
        if not positionals:
            return _terminal_classification(
                False,
                "terminal.rg",
                MEDIUM,
                "rg 缺少可静态识别的搜索表达式",
            )
        target_paths = positionals[1:]
    if any(not _is_static_path_token(path) for path in target_paths):
        return _terminal_classification(
            False,
            "terminal.rg",
            HIGH,
            "rg 的目标路径需要 Shell 动态展开",
        )
    return _terminal_classification(
        True,
        "terminal.rg",
        MEDIUM,
        "命令命中安全只读 rg 白名单",
        target_paths,
    )


def _classify_simple_terminal_tokens(
    tokens: Sequence[str],
) -> TerminalCommandClassification:
    """对已经确认无动态 Shell 结构的单个 argv 命令做白名单分类。"""
    if not tokens or _SHELL_ASSIGNMENT_RE.match(tokens[0]):
        return _terminal_classification(
            False,
            "terminal.shell",
            HIGH,
            "命令包含环境赋值或缺少可识别命令",
        )
    if any(token.startswith("~") for token in tokens):
        return _terminal_classification(
            False,
            "terminal.shell",
            HIGH,
            "命令包含依赖用户目录状态的 Shell 路径展开",
        )
    if list(tokens) == ["pwd"]:
        return _terminal_classification(
            True,
            "terminal.pwd",
            LOW,
            "命令命中 pwd 白名单",
        )
    if tokens[0] == "cd":
        target = None
        if len(tokens) == 1:
            target = "~"
        elif len(tokens) == 2:
            target = tokens[1]
        elif len(tokens) == 3 and tokens[1] == "--":
            target = tokens[2]
        if target is not None and (
            target == "~" or _is_static_path_token(target)
        ):
            return _terminal_classification(
                True,
                "terminal.cd",
                LOW,
                "命令命中简单 cd 白名单",
                (target,),
            )
        return _terminal_classification(
            False,
            "terminal.cd",
            HIGH,
            "cd 目标依赖动态状态或无法静态解析",
        )
    if tokens[0] == "ls":
        return _classify_ls(tokens[1:])
    if tokens[0] == "tail":
        return _classify_tail(tokens[1:])
    if tokens[0] == "stat":
        return _classify_stat(tokens[1:])
    if tokens[0] == "readlink":
        return _classify_static_query(
            "readlink",
            tokens[1:],
            short_options="fenqsvz",
            long_options=(
                "--canonicalize",
                "--canonicalize-existing",
                "--canonicalize-missing",
                "--no-newline",
                "--quiet",
                "--silent",
                "--verbose",
                "--zero",
            ),
            require_operands=True,
            target_paths=True,
            risk_level=MEDIUM,
        )
    if tokens[0] == "realpath":
        return _classify_static_query(
            "realpath",
            tokens[1:],
            short_options="eLmPqsz",
            long_options=(
                "--canonicalize-existing",
                "--canonicalize-missing",
                "--logical",
                "--physical",
                "--quiet",
                "--strip",
                "--zero",
            ),
            require_operands=True,
            target_paths=True,
            risk_level=LOW,
        )
    if tokens[0] in {"basename", "dirname"}:
        return _classify_static_query(
            tokens[0],
            tokens[1:],
            short_options="z",
            long_options=("--zero",),
            require_operands=True,
            risk_level=LOW,
        )
    if list(tokens) == ["whoami"]:
        return _terminal_classification(
            True,
            "terminal.whoami",
            LOW,
            "命令命中当前用户身份查询白名单",
        )
    if tokens[0] == "uname":
        return _classify_static_query(
            "uname",
            tokens[1:],
            short_options="asnrvmpio",
            long_options=(
                "--all",
                "--kernel-name",
                "--nodename",
                "--kernel-release",
                "--kernel-version",
                "--machine",
                "--processor",
                "--hardware-platform",
                "--operating-system",
            ),
            allow_operands=False,
            risk_level=LOW,
        )
    if tokens[0] == "which":
        return _classify_static_query(
            "which",
            tokens[1:],
            short_options="as",
            require_operands=True,
            risk_level=LOW,
        )
    if tokens[0] == "type":
        return _classify_static_query(
            "type",
            tokens[1:],
            short_options="aPpt",
            require_operands=True,
            risk_level=LOW,
        )
    if tokens[0] == "command":
        if (
            len(tokens) >= 3
            and tokens[1] in {"-v", "-V"}
            and all(_is_static_path_token(token) for token in tokens[2:])
        ):
            return _terminal_classification(
                True,
                "terminal.command_lookup",
                LOW,
                "命令命中 command 可执行文件查询白名单",
            )
        return _terminal_classification(
            False,
            "terminal.command",
            HIGH,
            "command 仅允许 -v/-V 查询形式自动执行",
        )
    if tokens[0] == "df":
        return _classify_static_query(
            "df",
            tokens[1:],
            short_options="ahHiklPT",
            long_options=(
                "--all",
                "--human-readable",
                "--si",
                "--inodes",
                "--local",
                "--portability",
                "--print-type",
                "--total",
                "--sync",
                "--no-sync",
            ),
            target_paths=True,
            risk_level=LOW,
        )
    if tokens[0] == "git":
        return _classify_git(tokens[1:])
    if tokens[0] == "rg":
        return _classify_rg(tokens[1:])
    if tokens[0] == "find":
        return _classify_find(tokens[1:])
    if tokens[0] == "du":
        return _classify_du(tokens[1:])
    if tokens[0] in {"grep", "head", "wc"}:
        return _classify_readonly_filter(tokens[0], tokens[1:])
    if tokens[0] in {"echo", "true", "false"}:
        return _terminal_classification(
            True,
            f"terminal.{tokens[0]}",
            LOW,
            f"命令命中无文件写入的 {tokens[0]} 白名单",
        )
    return _terminal_classification(
        False,
        f"terminal.{tokens[0]}",
        HIGH,
        "命令不在简单只读白名单中",
    )


def classify_terminal_command(command: object) -> TerminalCommandClassification:
    """保守识别可免审命令；任何静态不确定性都返回非自动放行。"""
    try:
        normalized = normalize_terminal_command(command).strip()
    except ValueError:
        return _terminal_classification(
            False,
            "terminal.shell",
            HIGH,
            "命令为空或格式无效，无法静态确认安全性",
        )
    if any(ord(char) < 32 and char not in "\t " for char in normalized):
        return _terminal_classification(
            False,
            "terminal.shell",
            HIGH,
            "命令包含无法静态解释的控制字符",
        )
    structure = _split_static_shell_segments(normalized)
    if structure is None:
        return _terminal_classification(
            False,
            "terminal.shell",
            HIGH,
            "命令包含动态 Shell、后台执行、写重定向或无法解析的语法",
        )
    segments, operators = structure
    classifications: list[TerminalCommandClassification] = []
    for segment in segments:
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            return _terminal_classification(
                False,
                "terminal.shell",
                HIGH,
                "命令词法解析失败，无法静态确认安全性",
            )
        classification = _classify_simple_terminal_tokens(tokens)
        if not classification.automatically_allowed:
            return classification
        classifications.append(classification)

    if not operators:
        return classifications[0]
    risk_level = max(
        (item.risk_level for item in classifications),
        key=lambda risk: _RISK_ORDER[risk],
    )
    target_paths = tuple(dict.fromkeys(
        path
        for item in classifications
        for path in item.target_paths
    ))
    is_pipeline_only = all(operator == "|" for operator in operators)
    return _terminal_classification(
        True,
        (
            "terminal.readonly_pipeline"
            if is_pipeline_only
            else "terminal.readonly_compound"
        ),
        risk_level,
        "复合命令的每个阶段均命中已知只读白名单",
        target_paths,
    )


def is_cwd_only_terminal_command(command: object) -> bool:
    """兼容判断：仅纯 ``cd`` / ``pwd`` 视为 cwd 命令。"""
    classification = classify_terminal_command(command)
    return (
        classification.automatically_allowed
        and classification.operation_type in {"terminal.cd", "terminal.pwd"}
    )


def approval_grant_identity_matches(
    approval_grant: object,
    tool_name: str,
    arguments: dict,
) -> bool:
    """校验审批编号、工具名和模型原始完整参数的基本绑定。"""
    if not _is_trusted_approval_grant(approval_grant):
        return False
    return (
        approval_grant.request_id.startswith("approval_")
        and approval_grant.tool_name == tool_name
        and approval_grant.arguments == arguments
    )


def approved_file_path_candidate(
    approval_grant: object,
    arguments: dict,
    *,
    session_key: str,
) -> str | None:
    """仅从已绑定同一 File 参数的内部 grant 读取审批绝对路径候选。"""
    try:
        normalized_session_key = normalize_approval_session_key(session_key)
    except ValueError:
        return None
    if (
        not approval_grant_identity_matches(
            approval_grant,
            "file",
            arguments,
        )
        or approval_grant.session_key != normalized_session_key
    ):
        return None
    approved_path = approval_grant.approved_abs_path
    if isinstance(approved_path, str) and approved_path:
        return approved_path
    return None


def approved_file_snapshot_candidate(
    approval_grant: object,
    arguments: dict,
    *,
    session_key: str,
) -> dict | None:
    """读取可信 File grant 绑定的获批文件状态快照。"""
    try:
        normalized_session_key = normalize_approval_session_key(session_key)
    except ValueError:
        return None
    if (
        not approval_grant_identity_matches(
            approval_grant,
            "file",
            arguments,
        )
        or approval_grant.session_key != normalized_session_key
    ):
        return None
    snapshot = approval_grant.file_snapshot
    return dict(snapshot) if isinstance(snapshot, dict) else None


def _file_fingerprint_payload(
    arguments: dict,
    normalized_path: str | None,
    file_snapshot: dict | None,
) -> dict:
    """快照型写操作使用 v2 指纹，其余保持 v1 兼容。"""
    payload = {
        "version": 1,
        "tool_name": "file",
        "arguments": dict(arguments),
        "abs_path": normalized_path,
    }
    if file_snapshot is not None:
        payload["version"] = 2
        payload["file_snapshot"] = normalize_file_state_snapshot(
            file_snapshot
        )
    return payload


_GATEWAY_SEND_FILE_IDENTITY_FIELDS = (
    "session_key_fingerprint",
    "route_key_fingerprint",
    "source_message_fingerprint",
    "chat_id_fingerprint",
    "reply_to_message_fingerprint",
    "thread_id_fingerprint",
)


def _normalize_gateway_send_file_snapshot(value: object) -> dict:
    """校验审批持久层中的出站文件快照，不读取文件正文。"""
    if not isinstance(value, Mapping):
        raise ValueError("gateway send file snapshot must be an object")
    abs_path = value.get("abs_path")
    sha256 = value.get("sha256")
    if not isinstance(abs_path, str) or not abs_path:
        raise ValueError("gateway send file snapshot path is invalid")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("gateway send file snapshot sha256 is invalid")
    normalized = {"abs_path": abs_path, "sha256": sha256}
    for field_name in (
        "size_bytes",
        "device",
        "inode",
        "mtime_ns",
        "ctime_ns",
    ):
        raw = value.get(field_name)
        if isinstance(raw, bool):
            raise ValueError(
                f"gateway send file snapshot {field_name} is invalid"
            )
        try:
            parsed = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"gateway send file snapshot {field_name} is invalid"
            ) from exc
        if parsed < 0 or (field_name == "size_bytes" and parsed <= 0):
            raise ValueError(
                f"gateway send file snapshot {field_name} is invalid"
            )
        normalized[field_name] = parsed
    return normalized


def _gateway_send_file_identity_details(
    *,
    session_key: str,
    route_key: str,
    source_message_id: str,
    platform: str,
    chat_id: str,
    reply_to_message_id: str | None,
    thread_id: str | None,
) -> dict:
    """把平台身份收敛为审批可持久化但不可逆推出原值的摘要。"""
    values = {
        "session_key": session_key,
        "route_key": route_key,
        "source_message": source_message_id,
        "platform": platform,
        "chat_id": chat_id,
    }
    if any(not isinstance(value, str) or not value.strip() for value in values.values()):
        raise ValueError("gateway send file identity is incomplete")
    return {
        "session_key_fingerprint": _identifier_fingerprint(session_key),
        "route_key_fingerprint": _identifier_fingerprint(route_key),
        "source_message_fingerprint": _identifier_fingerprint(
            source_message_id
        ),
        "platform": platform.strip().lower(),
        "chat_id_fingerprint": _identifier_fingerprint(chat_id),
        "reply_to_message_fingerprint": (
            _identifier_fingerprint(reply_to_message_id)
            if isinstance(reply_to_message_id, str) and reply_to_message_id
            else None
        ),
        "thread_id_fingerprint": (
            _identifier_fingerprint(thread_id)
            if isinstance(thread_id, str) and thread_id
            else None
        ),
    }


def _gateway_send_file_fingerprint_payload(
    arguments: dict,
    file_snapshot: dict,
    identity_details: Mapping,
) -> dict:
    """生成只绑定摘要身份、不持久化完整聊天标识的任务指纹。"""
    return {
        "version": 1,
        "tool_name": "gateway_send_file",
        "arguments": dict(arguments),
        "file_snapshot": _normalize_gateway_send_file_snapshot(
            file_snapshot
        ),
        "target_identity": {
            field_name: identity_details.get(field_name)
            for field_name in _GATEWAY_SEND_FILE_IDENTITY_FIELDS
        },
        "platform": identity_details.get("platform"),
    }


def assess_gateway_send_file(
    arguments: dict,
    *,
    file_snapshot: dict,
    session_key: str,
    route_key: str,
    source_message_id: str,
    platform: str,
    chat_id: str,
    reply_to_message_id: str | None,
    thread_id: str | None,
    remote_approval: bool,
    approval_grant: object = None,
) -> ApprovalAssessment:
    """出站文件每次都要求一次性审批，并绑定文件与目标会话摘要。"""
    normalized_arguments = dict(arguments)
    normalized_session_key = normalize_approval_session_key(session_key)
    normalized_snapshot = _normalize_gateway_send_file_snapshot(
        file_snapshot
    )
    identity = _gateway_send_file_identity_details(
        session_key=normalized_session_key,
        route_key=route_key,
        source_message_id=source_message_id,
        platform=platform,
        chat_id=chat_id,
        reply_to_message_id=reply_to_message_id,
        thread_id=thread_id,
    )
    fingerprint = _canonical_fingerprint(
        _gateway_send_file_fingerprint_payload(
            normalized_arguments,
            normalized_snapshot,
            identity,
        )
    )
    grant_matches = (
        approval_grant_identity_matches(
            approval_grant,
            "gateway_send_file",
            arguments,
        )
        and approval_grant.scope == "once"
        and approval_grant.session_key == normalized_session_key
        and approval_grant.fingerprint == fingerprint
        and approval_grant.approved_abs_path == normalized_snapshot["abs_path"]
        and approval_grant.file_snapshot == normalized_snapshot
    )
    if grant_matches:
        decision = ALLOW
        reason = "一次性审批与当前文件快照和目标会话完全一致"
        error_type = None
        error = None
        fatal = False
        decision_source = "once_grant"
    elif approval_grant is not None:
        decision = DENY
        reason = "出站文件审批已过期或文件、目标身份发生变化"
        error_type = "approval_stale"
        error = "approved file or Gateway target changed; request approval again"
        fatal = False
        decision_source = "grant_validation"
    elif remote_approval:
        decision = ASK
        reason = "向平台会话发送本地文件属于受控副作用"
        error_type = None
        error = None
        fatal = False
        decision_source = "approval_policy"
    else:
        decision = DENY
        reason = "出站文件只允许通过 Gateway 远程审批链执行"
        error_type = "forbidden"
        error = "gateway_send_file requires a Gateway remote approval context"
        fatal = True
        decision_source = "gateway_context"

    details = {
        "operation_type": "messaging.send_file",
        "target_path": normalized_snapshot["abs_path"],
        "abs_path": normalized_snapshot["abs_path"],
        "file_snapshot": normalized_snapshot,
        "size_bytes": normalized_snapshot["size_bytes"],
        "sha256": normalized_snapshot["sha256"],
        "target_platform": identity["platform"],
        "target_chat_fingerprint": identity["chat_id_fingerprint"],
        "reason": reason,
        "fingerprint": fingerprint,
        "risk_level": HIGH.value,
        "allowed_grant_scopes": list(allowed_grant_scopes(HIGH)),
        "backend_risk": _backend_fingerprint_payload({
            "backend_type": "gateway",
        }),
        "decision_source": decision_source,
        **identity,
    }
    return ApprovalAssessment(
        tool_name="gateway_send_file",
        decision=decision,
        risk_level=HIGH,
        fingerprint=fingerprint,
        reason=reason,
        normalized_arguments=normalized_arguments,
        details=details,
        normalized_path=normalized_snapshot["abs_path"],
        session_key=normalized_session_key,
        error_type=error_type,
        error=error,
        fatal=fatal,
    )


def approval_request_binding_matches(
    tool_name: str,
    arguments: dict,
    details: object,
    *,
    session_key: object,
) -> bool:
    """校验 Tool Result 中的审批身份能由原始调用和当前会话重建。"""
    if not isinstance(details, dict):
        return False
    risk_level = details.get("risk_level")
    if risk_level not in {level.value for level in ApprovalRiskLevel}:
        return False
    if details.get("allowed_grant_scopes") != list(
        allowed_grant_scopes(risk_level)
    ):
        return False
    backend_risk = details.get("backend_risk")
    if (
        not isinstance(backend_risk, dict)
        or backend_risk != _backend_fingerprint_payload(backend_risk)
    ):
        return False
    if not isinstance(details.get("decision_source"), str):
        return False
    fingerprint = details.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint.startswith(
        "sha256:"
    ):
        return False

    if tool_name == "gateway_send_file":
        try:
            normalized_session_key = normalize_approval_session_key(
                session_key
            )
            file_snapshot = _normalize_gateway_send_file_snapshot(
                details.get("file_snapshot")
            )
        except ValueError:
            return False
        if (
            details.get("abs_path") != file_snapshot["abs_path"]
            or details.get("size_bytes") != file_snapshot["size_bytes"]
            or details.get("sha256") != file_snapshot["sha256"]
            or details.get("session_key_fingerprint")
            != _identifier_fingerprint(normalized_session_key)
            or not isinstance(details.get("platform"), str)
            or details.get("target_platform") != details.get("platform")
            or details.get("target_chat_fingerprint")
            != details.get("chat_id_fingerprint")
            or any(
                not isinstance(details.get(field_name), str)
                for field_name in (
                    "route_key_fingerprint",
                    "source_message_fingerprint",
                    "chat_id_fingerprint",
                )
            )
        ):
            return False
        for field_name in _GATEWAY_SEND_FILE_IDENTITY_FIELDS:
            value = details.get(field_name)
            if value is not None and (
                not isinstance(value, str)
                or not value.startswith("sha256:")
            ):
                return False
        expected = _canonical_fingerprint(
            _gateway_send_file_fingerprint_payload(
                arguments,
                file_snapshot,
                details,
            )
        )
        return fingerprint == expected

    if tool_name == "cron":
        try:
            normalized_session_key = normalize_approval_session_key(session_key)
        except ValueError:
            return False
        if (
            details.get("operation_type") != "cron.capability_grant"
            or details.get("session_key_fingerprint")
            != _identifier_fingerprint(normalized_session_key)
            or details.get("allowed_grant_scopes") != ["once"]
        ):
            return False
        expected = _canonical_fingerprint({
            "version": 1,
            "tool_name": "cron",
            "arguments": arguments,
            "session_key": normalized_session_key,
            "backend_risk": backend_risk,
        })
        return fingerprint == expected

    if tool_name == "file":
        normalized_path = details.get("abs_path")
        if not isinstance(normalized_path, str) or not normalized_path:
            return False
        try:
            file_snapshot = details.get("file_snapshot")
            if file_snapshot is not None:
                file_snapshot = normalize_file_state_snapshot(file_snapshot)
                if file_snapshot["abs_path"] != normalized_path:
                    return False
            if _file_snapshot_required(arguments) and file_snapshot is None:
                return False
        except ValueError:
            return False
        expected = _canonical_fingerprint(_file_fingerprint_payload(
            arguments,
            normalized_path,
            file_snapshot,
        ))
        return fingerprint == expected

    if tool_name == "terminal":
        try:
            normalized_command = normalize_terminal_command(
                arguments.get("command")
            )
            normalized_session_key = normalize_approval_session_key(
                session_key
            )
        except ValueError:
            return False
        normalized_cwd = details.get("cwd")
        if not isinstance(normalized_cwd, str) or not normalized_cwd:
            return False
        if details.get("normalized_command") != normalized_command:
            return False
        if details.get("session_key_fingerprint") != _identifier_fingerprint(
            normalized_session_key
        ):
            return False
        expected = _canonical_fingerprint({
            "version": 2,
            "tool_name": "terminal",
            "command": normalized_command,
            "cwd": normalized_cwd,
            "session_key": normalized_session_key,
            "backend_risk": backend_risk,
        })
        return fingerprint == expected

    return False


def _terminal_grant_matches(
    approval_grant: object,
    arguments: dict,
    *,
    fingerprint: str,
    normalized_command: str,
    normalized_cwd: str,
    session_key: str,
) -> bool:
    """Terminal grant 必须覆盖命令、cwd、会话和统一指纹。"""
    return (
        approval_grant_identity_matches(
            approval_grant,
            "terminal",
            arguments,
        )
        and approval_grant.fingerprint == fingerprint
        and approval_grant.normalized_command == normalized_command
        and approval_grant.cwd == normalized_cwd
        and approval_grant.session_key == session_key
    )


def _file_grant_matches(
    approval_grant: object,
    arguments: dict,
    *,
    fingerprint: str,
    normalized_path: str | None,
    session_key: str,
) -> bool:
    """File grant 必须覆盖完整参数、规范化绝对路径和统一指纹。"""
    return (
        approval_grant_identity_matches(
            approval_grant,
            "file",
            arguments,
        )
        and approval_grant.fingerprint == fingerprint
        and approval_grant.approved_abs_path == normalized_path
        and approval_grant.session_key == session_key
    )


def assess_terminal_operation(
    arguments: dict,
    *,
    normalized_cwd: str,
    session_key: str,
    remote_approval: bool,
    interactive_approval: bool,
    approval_grant: object = None,
    dangerous_matches: Sequence[tuple[int, str, str]] = (),
    security_policy: ApprovalSecurityPolicy | None = None,
    backend_context: Mapping | None = None,
    intelligent_advisor: IntelligentApprovalAdvisor | None = None,
) -> ApprovalAssessment:
    """对 Terminal 操作生成唯一决策，不把 cwd 当成访问根目录。"""
    normalized_command = normalize_terminal_command(arguments.get("command"))
    normalized_session_key = normalize_approval_session_key(session_key)
    normalized_cwd = str(normalized_cwd or "").strip()
    if not normalized_cwd:
        raise ValueError("cwd must be a non-empty string")

    normalized_arguments = dict(arguments)
    normalized_arguments["command"] = normalized_command
    active_security_policy = (
        security_policy or DEFAULT_APPROVAL_SECURITY_POLICY
    )
    safe_backend_context = _backend_fingerprint_payload(backend_context)
    fingerprint = _canonical_fingerprint({
        "version": 2,
        "tool_name": "terminal",
        "command": normalized_command,
        "cwd": normalized_cwd,
        "session_key": normalized_session_key,
        "backend_risk": safe_backend_context,
    })
    command_classification = classify_terminal_command(normalized_command)
    detected_dangerous = tuple(
        dangerous_matches or detect_dangerous_command(normalized_command)
    )
    backend_risk = assess_backend_risk(backend_context)
    policy_denial = active_security_policy.terminal_denial(
        normalized_command,
        cwd=normalized_cwd,
        backend_context=backend_context,
    )
    if policy_denial is not None or detected_dangerous:
        risk_level = CRITICAL
    else:
        risk_level = _max_risk(
            command_classification.risk_level,
            backend_risk.risk_floor,
        )

    grant_matches = _terminal_grant_matches(
        approval_grant,
        arguments,
        fingerprint=fingerprint,
        normalized_command=normalized_command,
        normalized_cwd=normalized_cwd,
        session_key=normalized_session_key,
    )
    if policy_denial is not None:
        decision = DENY
        reason = policy_denial.reason
        error_type = policy_denial.error_type
        error = policy_denial.error
        fatal = True
        decision_source = policy_denial.decision_source
    elif detected_dangerous:
        decision = DENY
        reason = "critical Terminal 操作不允许创建 once 或 session grant"
        error_type = "safety_blocked"
        error = "critical terminal operation is denied by approval policy"
        fatal = True
        decision_source = "approval_policy"
    elif backend_risk.risk_floor == CRITICAL:
        decision = DENY
        reason = backend_risk.reason
        error_type = "backend_risk_denied"
        error = "terminal backend risk is critical and cannot be approved"
        fatal = True
        decision_source = "backend_risk"
    elif grant_matches:
        decision = ALLOW
        reason = "已批准操作与当前 Terminal 命令身份完全一致"
        error_type = None
        error = None
        fatal = False
        decision_source = "once_grant"
    elif approval_grant is not None:
        decision = DENY
        reason = "Terminal approval grant 与当前操作身份不一致"
        error_type = "approval_grant_mismatch"
        error = "approval grant does not match this terminal operation"
        fatal = True
        decision_source = "grant_validation"
    elif terminal_session_grant_matches(
        session_key=normalized_session_key,
        command=normalized_command,
        cwd=normalized_cwd,
        risk_level=risk_level,
    ):
        decision = ALLOW
        reason = "命令匹配当前 session 的结构化 Terminal grant"
        error_type = None
        error = None
        fatal = False
        decision_source = "session_grant"
    elif (
        remote_approval
        and backend_risk.automatic_allowance
        and command_classification.automatically_allowed
    ):
        decision = ALLOW
        reason = command_classification.reason
        error_type = None
        error = None
        fatal = False
        decision_source = "static_allowlist"
    elif remote_approval:
        decision = ASK
        reason = (
            backend_risk.reason
            if backend_risk.risk_floor == HIGH
            else command_classification.reason
        )
        error_type = None
        error = None
        fatal = False
        decision_source = "approval_policy"
    else:
        decision = ALLOW
        reason = "本地普通 Terminal 操作保持现有直接执行语义"
        error_type = None
        error = None
        fatal = False
        decision_source = "local_direct"

    details = {
        "command": normalized_command,
        "normalized_command": normalized_command,
        "cwd": normalized_cwd,
        "operation_type": command_classification.operation_type,
        "target_paths": list(command_classification.target_paths),
        "reason": reason,
        "session_key_fingerprint": _identifier_fingerprint(
            normalized_session_key
        ),
        "fingerprint": fingerprint,
        "risk_level": risk_level.value,
        "allowed_grant_scopes": list(allowed_grant_scopes(risk_level)),
        "backend_risk": safe_backend_context,
        "decision_source": decision_source,
    }

    assessment = ApprovalAssessment(
        tool_name="terminal",
        decision=decision,
        risk_level=risk_level,
        fingerprint=fingerprint,
        reason=reason,
        normalized_arguments=normalized_arguments,
        details=details,
        normalized_command=normalized_command,
        normalized_cwd=normalized_cwd,
        session_key=normalized_session_key,
        error_type=error_type,
        error=error,
        fatal=fatal,
    )
    return apply_intelligent_approval(
        assessment,
        security_policy=active_security_policy,
        advisor=intelligent_advisor,
    )


def assess_file_operation(
    arguments: dict,
    *,
    normalized_path: str | None,
    session_key: str,
    remote_approval: bool,
    sensitive: bool,
    allow_sensitive: bool,
    approval_grant: object = None,
    file_snapshot: dict | None = None,
    security_policy: ApprovalSecurityPolicy | None = None,
    backend_context: Mapping | None = None,
    intelligent_advisor: IntelligentApprovalAdvisor | None = None,
) -> ApprovalAssessment:
    """对 File 操作按完整参数和最终绝对路径生成唯一决策。"""
    normalized_arguments = dict(arguments)
    normalized_session_key = normalize_approval_session_key(session_key)
    action = str(arguments.get("action", ""))
    active_security_policy = (
        security_policy or DEFAULT_APPROVAL_SECURITY_POLICY
    )
    policy_denial = active_security_policy.file_denial(
        action=action,
        normalized_path=normalized_path,
    )
    if file_snapshot is not None:
        file_snapshot = normalize_file_state_snapshot(file_snapshot)
        if file_snapshot["abs_path"] != normalized_path:
            raise ValueError("file snapshot does not match normalized path")
    fingerprint = _canonical_fingerprint(_file_fingerprint_payload(
        normalized_arguments,
        normalized_path,
        file_snapshot,
    ))
    if policy_denial is not None or sensitive:
        risk_level = CRITICAL
    elif action in _FILE_WRITE_ACTIONS:
        risk_level = HIGH
    elif action in _FILE_READ_ACTIONS:
        risk_level = MEDIUM
    else:
        risk_level = LOW

    grant_matches = _file_grant_matches(
        approval_grant,
        arguments,
        fingerprint=fingerprint,
        normalized_path=normalized_path,
        session_key=normalized_session_key,
    )
    if policy_denial is not None:
        decision = DENY
        reason = policy_denial.reason
        error_type = policy_denial.error_type
        error = policy_denial.error
        fatal = True
        decision_source = policy_denial.decision_source
    elif sensitive:
        decision = DENY
        reason = "critical File 操作不允许创建 once 或 session grant"
        error_type = "sensitive_access_denied"
        error = "critical sensitive file operation is denied by approval policy"
        fatal = True
        decision_source = "approval_policy"
    elif grant_matches:
        decision = ALLOW
        reason = "已批准操作与当前 File 参数和目标路径完全一致"
        error_type = None
        error = None
        fatal = False
        decision_source = "once_grant"
    elif approval_grant is not None:
        decision = DENY
        reason = "File approval grant 与当前操作身份不一致"
        error_type = "approval_grant_mismatch"
        error = "approval grant does not match this file operation"
        fatal = True
        decision_source = "grant_validation"
    elif file_session_grant_matches(
        session_key=normalized_session_key,
        action=action,
        normalized_path=normalized_path,
        sensitive=sensitive,
        overwrite=_file_operation_mutates_existing(
            arguments,
            file_snapshot,
        ),
        risk_level=risk_level,
    ):
        decision = ALLOW
        reason = "操作匹配当前 session 的结构化 File grant"
        error_type = None
        error = None
        fatal = False
        decision_source = "session_grant"
    elif remote_approval and action in _FILE_WRITE_ACTIONS:
        decision = ASK
        reason = "File 写入或修改操作需要显式审批"
        error_type = None
        error = None
        fatal = False
        decision_source = "approval_policy"
    elif action in _FILE_READ_ACTIONS:
        decision = ALLOW
        reason = "普通文件只读操作不需要审批"
        error_type = None
        error = None
        fatal = False
        decision_source = "static_allowlist"
    elif action in _FILE_METADATA_ACTIONS:
        decision = ALLOW
        reason = "普通文件元数据操作不需要审批"
        error_type = None
        error = None
        fatal = False
        decision_source = "static_allowlist"
    else:
        decision = ALLOW
        reason = "File 上下文操作不需要审批"
        error_type = None
        error = None
        fatal = False
        decision_source = "static_allowlist"

    details = {
        "action": action,
        "operation_type": f"file.{action}",
        "target_path": normalized_path,
        "abs_path": normalized_path,
        "reason": reason,
        "fingerprint": fingerprint,
        "risk_level": risk_level.value,
        "allowed_grant_scopes": list(allowed_grant_scopes(risk_level)),
        "backend_risk": _backend_fingerprint_payload(backend_context),
        "decision_source": decision_source,
    }
    if file_snapshot is not None:
        details["file_snapshot"] = file_snapshot

    assessment = ApprovalAssessment(
        tool_name="file",
        decision=decision,
        risk_level=risk_level,
        fingerprint=fingerprint,
        reason=reason,
        normalized_arguments=normalized_arguments,
        details=details,
        normalized_path=normalized_path,
        session_key=normalized_session_key,
        error_type=error_type,
        error=error,
        fatal=fatal,
    )
    return apply_intelligent_approval(
        assessment,
        security_policy=active_security_policy,
        advisor=intelligent_advisor,
    )


def assess_path_policy_denial(
    tool_name: str,
    *,
    session_key: str | None = None,
) -> ApprovalAssessment:
    """把共享 denied_paths 命中收敛为不可审批的最高优先级拒绝。"""
    return ApprovalAssessment(
        tool_name=str(tool_name),
        decision=DENY,
        risk_level=CRITICAL,
        fingerprint="",
        reason="configured filesystem policy denied the referenced path",
        normalized_arguments={},
        details={"decision_source": "filesystem_policy"},
        session_key=session_key,
        error_type=PATH_POLICY_DENIED_ERROR_TYPE,
        error=(
            "terminal command references a path blocked by the configured "
            "filesystem policy"
            if tool_name == "terminal"
            else "path is blocked by the configured filesystem policy"
        ),
        fatal=True,
    )
