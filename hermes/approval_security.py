"""审批安全配置；具体规则由各工具审批模块解释。"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from hermes.path_policy import PathAccessPolicy


@dataclass(frozen=True, slots=True)
class PolicyDenial:
    """工具审批规则得到的不可审批拒绝。"""

    error_type: str
    reason: str
    error: str
    decision_source: str


@dataclass(frozen=True, slots=True)
class FileDenyRule:
    """用户配置的文件 action 与路径组合拒绝规则。"""

    actions: frozenset[str]
    path_under: str


def _path_is_under(path: str, parent: str) -> bool:
    try:
        return os.path.commonpath((path, parent)) == parent
    except (OSError, ValueError):
        return False


def _normalize_paths(
    values: Sequence[str],
    *,
    cwd: str,
    field_name: str,
) -> tuple[str, ...]:
    normalizer = PathAccessPolicy(())
    result: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"{field_name} entries must be non-empty strings "
                f"(invalid item at index {index})"
            )
        normalized = normalizer.normalize_path(value, cwd=cwd)
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


@dataclass(frozen=True, slots=True, init=False, repr=False)
class ApprovalSecurityPolicy:
    """保存工具 Handler 使用的用户安全配置。"""

    _denied_command_patterns: tuple[re.Pattern[str], ...]
    _denied_executables: frozenset[str]
    _protected_paths: tuple[str, ...]
    _hardline_protected_paths: tuple[str, ...]
    _denied_file_rules: tuple[FileDenyRule, ...]
    _approval_command_patterns: tuple[re.Pattern[str], ...]
    _approval_file_rules: tuple[FileDenyRule, ...]
    remote_default_allow: bool
    intelligent_approval_enabled: bool

    def __init__(
        self,
        *,
        denied_command_patterns: Sequence[str] = (),
        denied_executables: Sequence[str] = (),
        protected_paths: Sequence[str] = (),
        denied_file_rules: Sequence[Mapping] = (),
        approval_command_patterns: Sequence[str] = (),
        approval_file_rules: Sequence[Mapping] = (),
        hardline_protected_paths: Sequence[str] = (),
        remote_default_allow: bool = True,
        intelligent_approval_enabled: bool = False,
        cwd: str | None = None,
    ) -> None:
        def compile_patterns(
            values: Sequence[str], *, field_name: str
        ) -> tuple[re.Pattern[str], ...]:
            compiled: list[re.Pattern[str]] = []
            for index, value in enumerate(values):
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"{field_name} entries must be non-empty strings "
                        f"(invalid item at index {index})"
                    )
                try:
                    compiled.append(re.compile(value, re.IGNORECASE))
                except re.error as exc:
                    raise ValueError(
                        f"{field_name} contains an invalid regex "
                        f"at index {index}"
                    ) from exc
            return tuple(compiled)

        normalized_executables: set[str] = set()
        for index, executable in enumerate(denied_executables):
            if not isinstance(executable, str) or not executable.strip():
                raise ValueError(
                    "security.approval.denied_executables entries must be "
                    f"non-empty strings (invalid item at index {index})"
                )
            name = os.path.basename(executable.strip()).lower()
            if name.endswith(".exe"):
                name = name[:-4]
            normalized_executables.add(name)

        base_cwd = cwd if cwd is not None else os.getcwd()
        normalized_protected = _normalize_paths(
            protected_paths,
            cwd=base_cwd,
            field_name="security.approval.protected_paths",
        )
        normalized_hardline = _normalize_paths(
            hardline_protected_paths,
            cwd=base_cwd,
            field_name="hardline protected paths",
        )
        normalizer = PathAccessPolicy(())

        def compile_file_rules(
            rules: Sequence[Mapping], *, field_name: str
        ) -> tuple[FileDenyRule, ...]:
            normalized: list[FileDenyRule] = []
            allowed_actions = {
                "read",
                "read_range",
                "write",
                "append",
                "replace",
                "list",
                "stat",
            }
            for index, rule in enumerate(rules):
                if not isinstance(rule, Mapping):
                    raise ValueError(
                        f"{field_name} entries must be mappings "
                        f"(invalid item at index {index})"
                    )
                actions = rule.get("actions")
                path_under = rule.get("path_under")
                if (
                    not isinstance(actions, (list, tuple))
                    or not actions
                    or any(
                        not isinstance(action, str)
                        or action not in allowed_actions
                        for action in actions
                    )
                ):
                    raise ValueError(
                        f"{field_name} actions must be a non-empty File "
                        f"action list (invalid item at index {index})"
                    )
                if not isinstance(path_under, str) or not path_under.strip():
                    raise ValueError(
                        f"{field_name} path_under must be a non-empty "
                        f"string (invalid item at index {index})"
                    )
                normalized.append(
                    FileDenyRule(
                        actions=frozenset(actions),
                        path_under=normalizer.normalize_path(
                            path_under, cwd=base_cwd
                        ),
                    )
                )
            return tuple(normalized)

        if not isinstance(remote_default_allow, bool):
            raise ValueError(
                "security.approval.remote_default_allow must be a boolean"
            )
        if not isinstance(intelligent_approval_enabled, bool):
            raise ValueError(
                "security.approval.intelligent_approval.enabled must be a boolean"
            )
        object.__setattr__(
            self,
            "_denied_command_patterns",
            compile_patterns(
                denied_command_patterns,
                field_name="security.approval.denied_command_patterns",
            ),
        )
        object.__setattr__(
            self, "_denied_executables", frozenset(normalized_executables)
        )
        object.__setattr__(self, "_protected_paths", normalized_protected)
        object.__setattr__(
            self, "_hardline_protected_paths", normalized_hardline
        )
        object.__setattr__(
            self,
            "_denied_file_rules",
            compile_file_rules(
                denied_file_rules,
                field_name="security.approval.denied_file_rules",
            ),
        )
        object.__setattr__(
            self,
            "_approval_command_patterns",
            compile_patterns(
                approval_command_patterns,
                field_name="security.approval.approval_command_patterns",
            ),
        )
        object.__setattr__(
            self,
            "_approval_file_rules",
            compile_file_rules(
                approval_file_rules,
                field_name="security.approval.approval_file_rules",
            ),
        )
        object.__setattr__(self, "remote_default_allow", remote_default_allow)
        object.__setattr__(
            self, "intelligent_approval_enabled", intelligent_approval_enabled
        )

    def hardline_paths_intersecting_mount(self, path: str) -> tuple[str, ...]:
        return self._paths_intersecting_mount(
            path, self._hardline_protected_paths
        )

    def protected_paths_intersecting_mount(self, path: str) -> tuple[str, ...]:
        return self._paths_intersecting_mount(path, self._protected_paths)

    def is_hardline_protected_path(self, path: str) -> bool:
        return bool(self.hardline_paths_intersecting_mount(path))

    @staticmethod
    def _paths_intersecting_mount(
        path: str, candidates: Sequence[str]
    ) -> tuple[str, ...]:
        try:
            normalized = PathAccessPolicy(()).normalize_path(path)
        except ValueError:
            return ()
        return tuple(
            candidate
            for candidate in candidates
            if _path_is_under(normalized, candidate)
            or _path_is_under(candidate, normalized)
        )


DEFAULT_APPROVAL_SECURITY_POLICY = ApprovalSecurityPolicy()
