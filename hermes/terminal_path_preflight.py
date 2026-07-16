"""Local Terminal 命令中的明显路径引用预检。"""

from __future__ import annotations

import re
import shlex

from hermes.path_policy import PathAccessDeniedError, PathAccessPolicy


_CONTROL_TOKENS = {";", "&", "&&", "|", "||"}
_WINDOWS_ABSOLUTE_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])([A-Z]:[\\/][^\s;&|<>\"']+)"
)
_POSIX_ABSOLUTE_RE = re.compile(
    r"(?<![A-Za-z0-9_])(/[^\s;&|<>\"']+)"
)
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _strip_outer_quotes(token: str) -> str:
    """去掉词法切分保留的一层配对引号。"""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        return token[1:-1]
    return token


def _is_punctuation(token: str) -> bool:
    return bool(token) and all(char in ";&|<>" for char in token)


def _check_candidate(
    candidate: str,
    *,
    cwd: str,
    path_policy: PathAccessPolicy,
) -> None:
    """检查一个静态候选路径；无法可靠解释的文本由 Shell 自己处理。"""
    candidate = _strip_outer_quotes(candidate.strip())
    if not candidate or candidate in {"-", "--"}:
        return
    try:
        path_policy.require_allowed(candidate, cwd=cwd)
    except PathAccessDeniedError:
        raise
    except (OSError, ValueError):
        # 预检只拒绝能够确定命中的路径，不把解析失败伪装成策略拒绝。
        return


def _tokenize_best_effort(command: str) -> list[str]:
    """做轻量词法切分，不把结果视为完整 Bash 语法树。"""
    try:
        lexer = shlex.shlex(
            command,
            posix=False,
            punctuation_chars=";&|<>",
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return []


def preflight_terminal_command(
    command: str,
    *,
    cwd: str,
    path_policy: PathAccessPolicy,
) -> None:
    """拒绝命令中可静态确认的禁止路径引用。

    该检查只覆盖 cwd、简单 cd、参数和重定向等明显形式。Local Terminal
    路径执行仅为尽力预检，不是沙箱，复杂展开或动态脚本可能绕过它。
    """
    if not path_policy.denied_paths_configured:
        return

    path_policy.require_allowed(cwd, cwd=cwd)

    # 原始文本扫描用于保留未加引号 Windows 路径中的反斜杠。
    for pattern in (_WINDOWS_ABSOLUTE_RE, _POSIX_ABSOLUTE_RE):
        for match in pattern.finditer(command):
            _check_candidate(
                match.group(1),
                cwd=cwd,
                path_policy=path_policy,
            )

    tokens = _tokenize_best_effort(command)
    if not tokens:
        return

    # cd 在简单复合命令中也可能不是分段后的第一个 token，因此单独扫描。
    for index, raw_token in enumerate(tokens):
        if _strip_outer_quotes(raw_token) != "cd":
            continue
        target_index = index + 1
        if target_index < len(tokens) and tokens[target_index] == "--":
            target_index += 1
        if target_index >= len(tokens) or tokens[target_index] in _CONTROL_TOKENS:
            target = "~"
        else:
            target = tokens[target_index]
        if target != "-":
            _check_candidate(target, cwd=cwd, path_policy=path_policy)

    expect_command = True
    redirection_target = False
    for raw_token in tokens:
        token = _strip_outer_quotes(raw_token)
        if token in _CONTROL_TOKENS:
            expect_command = True
            redirection_target = False
            continue
        if _is_punctuation(token) and ("<" in token or ">" in token):
            redirection_target = True
            continue
        if redirection_target:
            _check_candidate(token, cwd=cwd, path_policy=path_policy)
            redirection_target = False
            continue
        if expect_command and _ASSIGNMENT_RE.match(token):
            continue
        if expect_command:
            # 以路径形式直接执行脚本或二进制时仍需检查。
            if token.startswith(("/", "~", ".", "\\")) or re.match(
                r"(?i)^[A-Z]:[\\/]",
                token,
            ):
                _check_candidate(token, cwd=cwd, path_policy=path_policy)
            expect_command = False
            continue
        if token in {"cd", "--"} or token.isdigit():
            continue
        if token.startswith("-"):
            if "=" in token:
                _, value = token.split("=", 1)
                _check_candidate(value, cwd=cwd, path_policy=path_policy)
            continue
        if "=" in token:
            _, value = token.split("=", 1)
            if value:
                _check_candidate(value, cwd=cwd, path_policy=path_policy)
        else:
            _check_candidate(token, cwd=cwd, path_policy=path_policy)
