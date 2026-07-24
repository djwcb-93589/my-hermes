"""Cron 无人值守能力授权的领域模型与运行时边界。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from hermes.path_utils import git_bash_to_windows_path

from hermes.tools.terminal_approval import classify_terminal_command
from hermes.cron.artifacts import cron_job_artifact_root


CRON_CAPABILITY_POLICY_VERSION = 1
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_WRITE_ACTIONS = frozenset({"write", "append", "replace", "delete", "move"})
_TERMINAL_BOUNDARY_FIELDS = frozenset({
    "terminal_allowed_executables",
    "terminal_allow_shell_operators",
    "terminal_allow_redirection",
    "terminal_allow_background",
    "terminal_allow_network",
    "terminal_allowed_workdirs",
})
_NETWORK_EXECUTABLES = frozenset({
    "curl", "wget", "ftp", "sftp", "scp", "ssh", "telnet", "nc",
    "ncat", "netcat", "ping", "tracert", "traceroute",
    "invoke-webrequest", "invoke-restmethod",
})
_NETWORK_ARGUMENT_RE = re.compile(r"(?:https?://|ftp://|\b(?:ssh|tcp|udp)://)", re.IGNORECASE)
_SHELL_OPERATOR_RE = re.compile(r"(?:\r|\n|&&|\|\||[;|`]|\$\(|\$\{|\b(?:if|then|fi|for|while|do|done)\b)")
_REDIRECTION_RE = re.compile(r"(?:\d?>>?|<<|&>)")
_BACKGROUND_RE = re.compile(r"(?<!&)&(?!&)|\b(?:nohup|start|start-process)\b", re.IGNORECASE)


def _canonical(value: Any) -> str:
    """生成稳定摘要，避免把原始敏感内容写入授权审计。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _normalise_path(value: str | os.PathLike[str]) -> str:
    """将授权路径固定为绝对规范路径，避免后续相对路径绕过。"""
    # 与 terminal/file 共用的 PathAccessPolicy.normalize_path 保持同一套
    # 路径语义：Windows 上先把 Git Bash 风格的 /e/双周报 转成 E:\双周报，
    # 再做 expandvars/expanduser/resolve。否则 Path("/e/双周报").resolve()
    # 会被当成相对当前盘符解析成 D:\e\双周报，与用户实际意图不符。
    expanded = os.path.expandvars(os.path.expanduser(str(value)))
    if os.name == "nt":
        expanded = git_bash_to_windows_path(expanded)
    return str(Path(expanded).resolve())


def _normalise_target(value: Any) -> dict:
    """只保留投递路由所需的非内容字段。"""
    # 接受任意 Mapping（包括 dict 和 MappingProxyType）；executor 把
    # delivery_target 包装成 MappingProxyType，但它不是 dict 子类，
    # 严格 isinstance(value, dict) 会把合法投递目标误判为空。
    if not isinstance(value, Mapping):
        return {}
    allowed = ("platform", "chat_id", "thread_id", "route_key", "target_id")
    return {key: str(value[key]) for key in allowed if value.get(key) not in (None, "")}


def _normalise_terminal_executables(value: Any) -> list[str]:
    """将终端可执行文件收敛为简单名称，避免路径形式绕过授权比较。"""
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError("terminal_allowed_executables must be a list of non-empty strings")
    names: set[str] = set()
    for item in value:
        name = item.strip().replace("\\", "/").rsplit("/", 1)[-1].lower()
        if not name or name in {".", ".."}:
            raise ValueError("terminal_allowed_executables contains an invalid executable")
        names.add(name)
    return sorted(names)


def _normalise_terminal_workdirs(value: Any, workdir: str) -> list[str]:
    """固定终端工作目录授权；此检查不是 Local Terminal 的沙箱。"""
    if value is None:
        return [workdir]
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError("terminal_allowed_workdirs must be a list of non-empty paths")
    return sorted({_normalise_path(item) for item in value})


def _terminal_scope(spec: dict, workdir: str) -> dict:
    """构造可持久化、可比较的终端最小权限约束。"""
    def enabled(name: str) -> bool:
        value = spec.get(name, False)
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be a boolean")
        return value

    return {
        "terminal_allowed_executables": _normalise_terminal_executables(
            spec.get("terminal_allowed_executables")
        ),
        "terminal_allow_shell_operators": enabled("terminal_allow_shell_operators"),
        "terminal_allow_redirection": enabled("terminal_allow_redirection"),
        "terminal_allow_background": enabled("terminal_allow_background"),
        "terminal_allow_network": enabled("terminal_allow_network"),
        "terminal_allowed_workdirs": _normalise_terminal_workdirs(
            spec.get("terminal_allowed_workdirs"), workdir
        ),
    }


def build_capability_scope(job: Any) -> dict:
    """从任务定义建立可比较的最小能力快照。"""
    spec = dict(getattr(job, "capability_spec", {}) or {})
    workdir = _normalise_path(getattr(job, "workdir", None) or os.getcwd())
    # 产物目录由系统配置统一决定；遗留的任务级 artifact_root 不能改变边界。
    artifact_root = _normalise_path(cron_job_artifact_root(str(job.job_id)))
    configured_roots = list(spec.get("allowed_roots") or [workdir]) + [artifact_root]
    allowed_roots = sorted({_normalise_path(root) for root in configured_roots})
    max_risk = str(spec.get("terminal_risk_max", "high")).lower()
    if max_risk not in {"low", "medium", "high"}:
        raise ValueError("Cron terminal_risk_max is invalid")
    terminal = _terminal_scope(spec, workdir)

    delivery_config = dict(getattr(job, "delivery_config", {}) or {})
    artifact_policy = dict(getattr(job, "artifact_policy", {}) or {})
    delivery_target = _normalise_target(
        spec.get("delivery_target") or delivery_config.get("target") or delivery_config.get("origin")
    )
    return {
        "job_id": str(job.job_id),
        "job_version": int(job.version),
        "policy_version": CRON_CAPABILITY_POLICY_VERSION,
        "prompt_digest": hashlib.sha256(str(job.prompt).encode("utf-8")).hexdigest(),
        "toolsets": sorted({str(item) for item in getattr(job, "toolsets", [])}),
        "skills": sorted({str(item) for item in getattr(job, "skills", [])}),
        "execution_environments": ["cron"],
        "workdir": workdir,
        "allowed_roots": allowed_roots,
        "artifact_root": artifact_root,
        "terminal_risk_max": max_risk,
        **terminal,
        "allow_file_write": bool(spec.get("allow_file_write", False)),
        "delivery_target": delivery_target,
        "delivery_config_digest": _digest(delivery_config),
        "artifact_policy_digest": _digest(artifact_policy),
        "allow_external_communication": bool(spec.get("allow_external_communication", bool(delivery_target))),
        "max_artifact_file_bytes": int(spec.get("max_artifact_file_bytes", 20 * 1024 * 1024)),
        "max_artifact_total_bytes": int(spec.get("max_artifact_total_bytes", 50 * 1024 * 1024)),
        "timeout_seconds": float(getattr(job, "execution_timeout_seconds", 300.0)),
    }


def capability_fingerprint(scope: dict) -> str:
    """计算授权能力快照的不可逆指纹。"""
    comparable = dict(scope)
    comparable.pop("job_version", None)
    return _digest(comparable)


def capability_change_requires_reauthorization(previous: Any, current: Any) -> bool:
    """判断更新是否扩大能力；名称、暂停和缩短超时不会使授权失效。"""
    before = build_capability_scope(previous)
    after = build_capability_scope(current)
    protected = (
        "prompt_digest",
        "toolsets",
        "skills",
        "workdir",
        "allowed_roots",
        "terminal_risk_max",
        "terminal_allowed_executables",
        "terminal_allow_shell_operators",
        "terminal_allow_redirection",
        "terminal_allow_background",
        "terminal_allow_network",
        "terminal_allowed_workdirs",
        "allow_file_write",
        "delivery_target",
        "delivery_config_digest",
        "artifact_policy_digest",
        "allow_external_communication",
        "max_artifact_file_bytes",
        "max_artifact_total_bytes",
    )
    if any(before[key] != after[key] for key in protected):
        return True
    return after["timeout_seconds"] > before["timeout_seconds"]


def build_cron_capability_grant(
    job: Any,
    *,
    creator_id: str,
    allowed_tool_names: set[str] | list[str],
    approval_id: str | None = None,
    scope: dict | None = None,
) -> dict:
    """构造持久 Cron grant；审计仅保存摘要和能力标识。"""
    expected_scope = build_capability_scope(job)
    scope = dict(scope or expected_scope)
    if scope != expected_scope:
        raise ValueError("Cron capability scope does not match the task definition")
    return {
        "grant_id": str(uuid.uuid4()),
        "job_id": scope["job_id"],
        "job_version": scope["job_version"],
        "policy_version": scope["policy_version"],
        "prompt_digest": scope["prompt_digest"],
        "capability_fingerprint": capability_fingerprint(scope),
        "scope": scope,
        "allowed_tool_names": sorted({str(name) for name in allowed_tool_names}),
        "creator_id": str(creator_id),
        "approval_id": approval_id,
        "audit": {
            "scope_digest": _digest(scope),
            "tool_count": len(set(allowed_tool_names)),
        },
    }


def validate_cron_capability_grant(job: Any, grant: dict | None, *, resolved_tool_names: set[str], context: Any) -> str | None:
    """返回安全的拒绝类别；成功时返回 ``None``。"""
    if not grant or str(grant.get("status", "active")) != "active":
        return "missing_or_inactive_grant"
    scope = dict(grant.get("scope") or {})
    expected = build_capability_scope(job)
    if int(grant.get("job_version", -1)) != int(job.version):
        return "job_version_mismatch"
    if int(grant.get("policy_version", -1)) != CRON_CAPABILITY_POLICY_VERSION:
        return "policy_version_mismatch"
    if grant.get("prompt_digest") != expected["prompt_digest"]:
        return "prompt_digest_mismatch"
    if scope != expected:
        # 旧数据库中的非 Terminal grant 没有新增字段，保留其原有只读边界；
        # 历史 Terminal grant 必须重新授权，不能用缺少可执行文件白名单的记录继续运行。
        legacy_expected = {
            key: value for key, value in expected.items()
            if key not in _TERMINAL_BOUNDARY_FIELDS
        }
        legacy_compatible = (
            "terminal" not in set(expected.get("toolsets", []))
            and not (_TERMINAL_BOUNDARY_FIELDS & set(scope))
            and scope == legacy_expected
            and grant.get("capability_fingerprint")
            == capability_fingerprint(legacy_expected)
        )
        if not legacy_compatible:
            return "scope_mismatch"
    elif grant.get("capability_fingerprint") != capability_fingerprint(expected):
        return "capability_mismatch"
    allowed_names = {str(name) for name in grant.get("allowed_tool_names", [])}
    if not allowed_names or not allowed_names.issubset(resolved_tool_names):
        return "tool_declaration_mismatch"
    if _normalise_path(context.workdir) != expected["workdir"]:
        return "workdir_mismatch"
    try:
        Path(context.artifact_dir).resolve().relative_to(
            Path(expected["artifact_root"]).resolve()
        )
    except ValueError:
        return "artifact_root_mismatch"
    if _normalise_target(context.delivery_target) != expected["delivery_target"]:
        return "delivery_target_mismatch"
    if float(context.timeout_seconds) > float(expected["timeout_seconds"]):
        return "timeout_mismatch"
    return None


@dataclass
class CronCapabilityGuard:
    """由可信 tool_context 注入的逐调用授权闸门。"""

    grant: dict
    violation: dict | None = None
    _allowed_names: set[str] = field(init=False, repr=False)
    _scope: dict = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._scope = dict(self.grant.get("scope") or {})
        self._allowed_names = {str(name) for name in self.grant.get("allowed_tool_names", [])}

    def _deny(self, tool_name: str, category: str) -> dict:
        if self.violation is None:
            self.violation = {"tool_name": tool_name, "category": category}
        return {
            "ok": False,
            "error_type": "cron_capability_denied",
            "error": "Cron capability grant does not authorize this operation. Update the task or request authorization again.",
        }

    def authorize_tool(self, tool_name: str) -> dict | None:
        if tool_name not in self._allowed_names:
            return self._deny(tool_name, "tool_not_granted")
        return None

    def authorize_skill(self, name: object) -> dict | None:
        """Cron 只能读取任务显式预加载的 Skill，不能借由 Skill 名称扩大能力。"""
        if not isinstance(name, str) or name not in set(self._scope.get("skills", [])):
            return self._deny("skill_view", "skill_not_granted")
        return None

    def authorize_terminal(self, command: str, *, cwd: str | None = None) -> dict | None:
        """在 backend 执行前检查 Cron 明确授予的单命令终端边界。"""
        denied = self.authorize_tool("terminal")
        if denied:
            return denied
        risk = str(classify_terminal_command(command).risk_level.value).lower()
        if _RISK_ORDER.get(risk, _RISK_ORDER["critical"]) > _RISK_ORDER[self._scope["terminal_risk_max"]]:
            return self._deny("terminal", "terminal_risk_exceeded")
        if _SHELL_OPERATOR_RE.search(command) and not bool(
            self._scope.get("terminal_allow_shell_operators")
        ):
            return self._deny("terminal", "terminal_shell_operator_not_granted")
        if _REDIRECTION_RE.search(command) and not bool(
            self._scope.get("terminal_allow_redirection")
        ):
            return self._deny("terminal", "terminal_redirection_not_granted")
        if _BACKGROUND_RE.search(command) and not bool(
            self._scope.get("terminal_allow_background")
        ):
            return self._deny("terminal", "terminal_background_not_granted")
        try:
            argv = shlex.split(command, posix=False)
        except ValueError:
            return self._deny("terminal", "terminal_command_not_auditable")
        if not argv:
            return self._deny("terminal", "terminal_command_not_auditable")
        executable = argv[0].strip().replace("\\", "/").rsplit("/", 1)[-1].lower()
        if executable not in set(self._scope.get("terminal_allowed_executables", [])):
            return self._deny("terminal", "terminal_executable_not_granted")
        git_network_action = executable == "git" and any(
            item.lower() in {"clone", "fetch", "pull", "push", "ls-remote"}
            for item in argv[1:3]
        )
        if (
            (executable in _NETWORK_EXECUTABLES or git_network_action or _NETWORK_ARGUMENT_RE.search(command))
            and not bool(self._scope.get("terminal_allow_network"))
        ):
            return self._deny("terminal", "terminal_network_not_granted")
        try:
            actual_cwd = _normalise_path(cwd or self._scope["workdir"])
            if not any(
                Path(actual_cwd).is_relative_to(Path(root).resolve())
                for root in self._scope.get("terminal_allowed_workdirs", [])
            ):
                return self._deny("terminal", "terminal_workdir_not_granted")
        except (OSError, ValueError):
            return self._deny("terminal", "terminal_workdir_not_granted")
        return None

    def authorize_file(self, action: str, path: str) -> dict | None:
        denied = self.authorize_tool("file")
        if denied:
            return denied
        if action in _WRITE_ACTIONS and not bool(self._scope.get("allow_file_write")):
            return self._deny("file", "file_write_not_granted")
        candidate = Path(path).resolve()
        for root in self._scope.get("allowed_roots", []):
            try:
                candidate.relative_to(Path(root).resolve())
                return None
            except ValueError:
                continue
        return self._deny("file", "file_root_not_granted")
