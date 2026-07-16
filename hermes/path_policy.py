"""模型可控本机路径的统一拒绝策略。"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass

from hermes.path_utils import git_bash_to_windows_path


PATH_POLICY_DENIED_ERROR_TYPE = "path_policy_denied"


class PathAccessDeniedError(Exception):
    """目标路径命中用户配置的硬拒绝规则。"""


@dataclass(frozen=True, slots=True, init=False, repr=False)
class PathAccessPolicy:
    """保存预规范化拒绝目录的不可变路径策略。"""

    _denied_paths: tuple[str, ...]

    def __init__(
        self,
        denied_paths: Iterable[str] = (),
        *,
        cwd: str | None = None,
    ) -> None:
        base_cwd = cwd if cwd is not None else os.getcwd()
        normalized: list[str] = []
        seen: set[str] = set()
        for index, path in enumerate(denied_paths):
            if not isinstance(path, str) or not path.strip():
                raise ValueError(
                    "security.filesystem.denied_paths entries must be "
                    f"non-empty strings (invalid item at index {index})"
                )
            value = self.normalize_path(path, cwd=base_cwd)
            if value not in seen:
                seen.add(value)
                normalized.append(value)
        object.__setattr__(self, "_denied_paths", tuple(normalized))

    @property
    def denied_paths_configured(self) -> bool:
        """是否至少配置了一条拒绝路径。"""
        return bool(self._denied_paths)

    @property
    def denied_paths_count(self) -> int:
        """返回拒绝路径数量，不暴露路径内容。"""
        return len(self._denied_paths)

    def normalize_path(self, path: str, *, cwd: str | None = None) -> str:
        """展开并返回供比较与执行共用的绝对真实路径。"""
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non-empty string")

        expanded = os.path.expandvars(os.path.expanduser(path))
        if os.name == "nt":
            expanded = git_bash_to_windows_path(expanded)

        if not os.path.isabs(expanded):
            base = cwd if cwd is not None else os.getcwd()
            if not isinstance(base, str) or not base.strip():
                raise ValueError("cwd must be a non-empty string")
            base = os.path.expandvars(os.path.expanduser(base))
            if os.name == "nt":
                base = git_bash_to_windows_path(base)
            base = os.path.abspath(base)
            expanded = os.path.join(base, expanded)

        return os.path.normcase(
            os.path.realpath(os.path.abspath(expanded))
        )

    def is_denied(self, path: str, *, cwd: str | None = None) -> bool:
        """判断目标是否等于拒绝目录或位于其子树中。"""
        target = self.normalize_path(path, cwd=cwd)
        return self._is_normalized_denied(target)

    def _is_normalized_denied(self, target: str) -> bool:
        """对已经规范化的路径执行唯一一套包含关系判断。"""
        for denied in self._denied_paths:
            try:
                if os.path.commonpath((target, denied)) == denied:
                    return True
            except ValueError:
                # Windows 不同盘符没有共同路径，继续检查其它规则。
                continue
        return False

    def require_allowed(self, path: str, *, cwd: str | None = None) -> str:
        """返回规范化路径；命中拒绝目录时抛出稳定策略异常。"""
        target = self.normalize_path(path, cwd=cwd)
        if self._is_normalized_denied(target):
            raise PathAccessDeniedError(
                "path is blocked by the configured filesystem policy"
            )
        return target


ALLOW_ALL_PATH_POLICY = PathAccessPolicy(())
