"""Terminal 工具专用的审批策略、Binding 与执行前复检。"""

from __future__ import annotations

import os
import posixpath
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Sequence

from hermes.approval_policy import (
    ALLOW,
    ASK,
    CRITICAL,
    DENY,
    HIGH,
    LOW,
    MEDIUM,
    ApprovalAssessment,
    ApprovalRiskLevel,
    IntelligentApprovalAdvisor,
    TrustedApprovalGrant,
    _approval_details,
    approval_binding_fingerprint,
    approval_grant_identity_matches,
    apply_intelligent_approval,
    normalize_approval_session_key,
    normalize_risk_level,
    session_grant_matches,
)
from hermes.approval_security import (
    ApprovalSecurityPolicy,
    DEFAULT_APPROVAL_SECURITY_POLICY,
    PolicyDenial,
)
from hermes.path_policy import (
    PATH_POLICY_DENIED_ERROR_TYPE,
    PathAccessPolicy,
)


_TOOL_NAME = "terminal"
_BINDING_FIELDS = frozenset({
    "normalized_command",
    "cwd",
    "backend_risk",
    "risk_level",
})


@dataclass(frozen=True, slots=True)
class TerminalCommandClassification:
    """保守静态分析得到的 Terminal 自动放行结论。"""

    automatically_allowed: bool
    operation_type: str
    risk_level: ApprovalRiskLevel
    reason: str
    target_paths: tuple[str, ...] = ()


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
class BackendRiskAssessment:
    """Terminal 使用的最小 backend 风险画像。"""

    backend_type: str
    risk_floor: ApprovalRiskLevel
    automatic_allowance: bool
    reason: str


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

def normalize_terminal_command(command: object) -> str:
    """统一换行表示，同时保留所有可能影响 Shell 语义的其它字符。"""
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a non-empty string")
    return command.replace("\r\n", "\n").replace("\r", "\n")

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

def _path_is_under(path: str, parent: str) -> bool:
    """使用 commonpath 判断结构化路径范围，兼容 Windows 不同盘符。"""
    try:
        return os.path.commonpath((
            os.path.normcase(path),
            os.path.normcase(parent),
        )) == os.path.normcase(parent)
    except ValueError:
        return False

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


def _terminal_policy_denial(
    policy: ApprovalSecurityPolicy,
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
        hardline_protected_paths=policy._hardline_protected_paths,
    )
    if hardline is not None:
        return hardline

    if any(pattern.search(command) for pattern in policy._denied_command_patterns):
        return PolicyDenial(
            error_type="configured_deny_rule",
            reason="命令命中用户配置的禁止命令规则",
            error="terminal command is blocked by a configured deny rule",
            decision_source="user_deny_rule",
        )

    executables = _extract_shell_executables(command)
    if any(
        _normalize_executable_name(executable) in policy._denied_executables
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
        candidate_paths=policy._protected_paths,
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


def _terminal_requires_approval(
    policy: ApprovalSecurityPolicy,
    command: str,
) -> bool:
    """返回命中远程审批黑名单的常规 Terminal 操作。"""
    return any(pattern.search(command) for pattern in policy._approval_command_patterns)


def _terminal_grant_matches(
    approval_grant: object,
    arguments: dict,
    *,
    fingerprint: str,
    binding: dict,
    session_key: str,
) -> bool:
    """一次性授权必须覆盖命令、cwd、backend、会话和统一指纹。"""
    return (
        approval_grant_identity_matches(
            approval_grant,
            _TOOL_NAME,
            arguments,
        )
        and approval_grant.fingerprint == fingerprint
        and approval_grant.session_key == session_key
        and approval_grant.binding == binding
    )


def _expected_binding_risk(
    normalized_command: str,
    backend_context: Mapping | None,
) -> ApprovalRiskLevel:
    classification = classify_terminal_command(normalized_command)
    if detect_dangerous_command(normalized_command):
        return CRITICAL
    return _max_risk(
        classification.risk_level,
        assess_backend_risk(backend_context).risk_floor,
    )


def _validate_terminal_binding(
    *,
    arguments: dict,
    binding: dict,
    session_key: str,
) -> bool:
    """重新构造 Terminal 身份，拒绝只满足字段类型的伪 Binding。"""
    if (
        not isinstance(arguments, dict)
        or not isinstance(binding, dict)
        or set(binding) != _BINDING_FIELDS
    ):
        return False
    try:
        normalize_approval_session_key(session_key)
        normalized_command = normalize_terminal_command(arguments.get("command"))
        cwd = str(binding.get("cwd") or "").strip()
        if not cwd or binding.get("cwd") != cwd:
            return False
        backend_context = binding.get("backend_risk")
        if (
            not isinstance(backend_context, dict)
            or backend_context != _backend_fingerprint_payload(backend_context)
            or binding.get("normalized_command") != normalized_command
        ):
            return False
        risk_level = normalize_risk_level(binding.get("risk_level"))
    except (AttributeError, TypeError, ValueError):
        return False
    return risk_level == _expected_binding_risk(
        normalized_command,
        backend_context,
    )


def build_terminal_session_rule(
    grant: TrustedApprovalGrant,
) -> TerminalSessionGrantRule | None:
    """从可信 Binding 构造低风险、精确 cwd 的结构化会话规则。"""
    if (
        grant.tool_name != _TOOL_NAME
        or grant.scope != "session"
        or not _validate_terminal_binding(
            arguments=grant.arguments,
            binding=grant.binding,
            session_key=grant.session_key,
        )
    ):
        return None
    try:
        risk_level = normalize_risk_level(grant.binding.get("risk_level"))
        command = normalize_terminal_command(
            grant.binding.get("normalized_command")
        )
        cwd = str(grant.binding.get("cwd") or "").strip()
        parsed = parse_terminal_command_for_grant(command)
    except (AttributeError, TypeError, ValueError):
        return None
    if (
        risk_level not in {LOW, MEDIUM}
        or not cwd
        or parsed is None
        or parsed.has_shell_operators
        or not parsed.executable
    ):
        return None
    return TerminalSessionGrantRule(
        executable=parsed.executable,
        argv_prefix=parsed.argv,
        cwd_policy="exact",
        cwd=cwd,
        allow_shell_operators=False,
        max_risk=risk_level,
    )


def terminal_session_rule_matches(
    rule: object,
    runtime_context: Mapping,
) -> bool:
    """执行前重新解析命令，并按 executable、argv、cwd 与风险复检。"""
    if not isinstance(rule, TerminalSessionGrantRule):
        return False
    try:
        command = normalize_terminal_command(runtime_context.get("command"))
        cwd = str(runtime_context.get("cwd") or "").strip()
        risk_level = normalize_risk_level(runtime_context.get("risk_level"))
        parsed = parse_terminal_command_for_grant(command)
    except (AttributeError, TypeError, ValueError):
        return False
    if parsed is None:
        return False
    if parsed.has_shell_operators and not rule.allow_shell_operators:
        return False
    if not parsed.executable or parsed.executable != rule.executable:
        return False
    if parsed.argv[:len(rule.argv_prefix)] != rule.argv_prefix:
        return False
    if rule.cwd_policy == "exact":
        if not rule.cwd or cwd != rule.cwd:
            return False
    elif rule.cwd_policy != "any":
        return False
    return _risk_at_most(risk_level, rule.max_risk)


class TerminalApprovalHandler:
    """解释 Terminal Binding，并提供结构化 session grant 规则。"""

    def validate_request_binding(
        self,
        *,
        arguments: dict,
        binding: dict,
        session_key: str,
    ) -> bool:
        return _validate_terminal_binding(
            arguments=arguments,
            binding=binding,
            session_key=session_key,
        )

    def validate_grant_binding(
        self,
        *,
        arguments: dict,
        binding: dict,
        session_key: str,
    ) -> bool:
        return _validate_terminal_binding(
            arguments=arguments,
            binding=binding,
            session_key=session_key,
        )

    def build_session_rule(
        self,
        grant: TrustedApprovalGrant,
    ) -> TerminalSessionGrantRule | None:
        return build_terminal_session_rule(grant)

    def session_rule_matches(
        self,
        rule: object,
        runtime_context: Mapping,
    ) -> bool:
        return terminal_session_rule_matches(rule, runtime_context)


_TERMINAL_APPROVAL_HANDLER = TerminalApprovalHandler()


def register_terminal_approval_handler() -> None:
    """随工具注册唯一的 Terminal 审批 Handler。"""
    from hermes.approval_handlers import (
        get_approval_handler,
        register_approval_handler,
    )

    registered = get_approval_handler("terminal")
    if registered is None:
        register_approval_handler(
            "terminal", _TERMINAL_APPROVAL_HANDLER
        )
    elif registered is not _TERMINAL_APPROVAL_HANDLER:
        raise ValueError("approval handler already registered: terminal")


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
    active_security_policy = security_policy or DEFAULT_APPROVAL_SECURITY_POLICY
    safe_backend_context = _backend_fingerprint_payload(backend_context)
    command_classification = classify_terminal_command(normalized_command)
    detected_dangerous = tuple(
        dangerous_matches or detect_dangerous_command(normalized_command)
    )
    backend_risk = assess_backend_risk(backend_context)
    policy_denial = _terminal_policy_denial(
        active_security_policy,
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

    binding = {
        "normalized_command": normalized_command,
        "cwd": normalized_cwd,
        "backend_risk": safe_backend_context,
        "risk_level": risk_level.value,
    }
    fingerprint = approval_binding_fingerprint(
        _TOOL_NAME,
        normalized_arguments,
        session_key=normalized_session_key,
        binding=binding,
    )

    grant_matches = _terminal_grant_matches(
        approval_grant,
        arguments,
        fingerprint=fingerprint,
        binding=binding,
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
        error_type = "approval_stale"
        error = "approved terminal operation changed; request approval again"
        fatal = False
        decision_source = "grant_validation"
    elif session_grant_matches(
        _TOOL_NAME,
        {
            "session_key": normalized_session_key,
            "command": normalized_command,
            "cwd": normalized_cwd,
            "risk_level": risk_level.value,
        },
    ):
        decision = ALLOW
        reason = "命令匹配当前 session 的结构化 Terminal grant"
        error_type = None
        error = None
        fatal = False
        decision_source = "session_grant"
    elif (
        remote_approval
        and active_security_policy.remote_default_allow
        and backend_risk.automatic_allowance
        and not _terminal_requires_approval(
            active_security_policy,
            normalized_command,
        )
    ):
        decision = ALLOW
        reason = "terminal operation is outside the remote approval blacklist"
        error_type = None
        error = None
        fatal = False
        decision_source = "remote_blacklist_default_allow"
    elif remote_approval:
        decision = ASK
        reason = (
            backend_risk.reason
            if backend_risk.risk_floor == HIGH
            else "terminal operation matches the remote approval blacklist"
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

    details, fingerprint = _approval_details(
        _TOOL_NAME,
        normalized_arguments,
        session_key=normalized_session_key,
        binding=binding,
        operation_type=command_classification.operation_type,
        risk_level=risk_level,
        reason=reason,
        decision_source=decision_source,
    )

    assessment = ApprovalAssessment(
        tool_name=_TOOL_NAME,
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


def assess_terminal_path_policy_denial(
    *,
    session_key: str | None = None,
) -> ApprovalAssessment:
    """把共享 denied_paths 命中收敛为 Terminal 的最高优先级拒绝。"""
    return ApprovalAssessment(
        tool_name=_TOOL_NAME,
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
        ),
        fatal=True,
    )
