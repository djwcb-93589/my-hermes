#!/usr/bin/env python3
"""在本地用户 Skill 目录安装、检查或卸载 Claude Code Skill。"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

import yaml


SKILL_NAME = "claude-code"
SCRIPT_PATH = Path(__file__).absolute()
SOURCE_DIR = SCRIPT_PATH.parent.parent
RUNTIME_ROOT_FILES = ("SKILL.md",)
RUNTIME_DIRS = ("references", "templates", "scripts", "assets")
ALLOWED_FRONTMATTER_FIELDS = frozenset(
    {"name", "description", "version", "platforms", "metadata"}
)
FORBIDDEN_RUNTIME_PARTS = frozenset(
    {
        "tests",
        "test",
        "results",
        "integration",
        "__pycache__",
        ".pytest_cache",
        ".git",
        ".github",
        ".hg",
        ".svn",
        ".mypy_cache",
        ".ruff_cache",
        ".tmp",
        "tmp",
        ".temp",
        "temp",
        "htmlcov",
    }
)
FORBIDDEN_RUNTIME_FILES = frozenset(
    {
        "ccs_p1_test_report.md",
        ".myhermes.json",
        ".gitignore",
        ".gitattributes",
        ".gitmodules",
        ".coverage",
        "coverage.xml",
        "conftest.py",
        "pytest.ini",
        "tox.ini",
    }
)
STAGING_PREFIX = ".claude-code-staging-"
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\((?P<target>[^)\n]+)\)")
WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")


class LocalSkillError(Exception):
    """表示可稳定报告给调用者的本地 Skill 操作错误。"""

    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type
        self.message = message


@dataclass(frozen=True)
class RuntimeFile:
    """记录一个运行时文件的稳定相对路径与摘要。"""

    relative_path: str
    absolute_path: Path
    sha256: str


@dataclass(frozen=True)
class PackageSnapshot:
    """记录经过验证的运行时 package 状态。"""

    files: tuple[RuntimeFile, ...]
    skill_revision: str
    package_revision: str


def _find_repository_root(start: Path) -> Path:
    """从脚本位置定位仓库，不依赖固定用户路径。"""

    for candidate in (start, *start.parents):
        if (
            (candidate / "pyproject.toml").is_file()
            and (candidate / "hermes" / "config_values.py").is_file()
        ):
            return candidate
    raise LocalSkillError(
        "repository_not_found",
        "my-hermes repository root could not be located from local_skill.py",
    )


REPOSITORY_ROOT = _find_repository_root(SCRIPT_PATH.parent)
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

# 允许从 integration 目录直接执行，同时复用仓库的轻量配置值逻辑。
hermes_home = importlib.import_module("hermes.config_values").hermes_home


BUNDLED_SKILLS_ROOT = (
    REPOSITORY_ROOT / "hermes" / "skills" / "bundled"
).resolve(strict=False)


def _lexists(path: Path) -> bool:
    """同时识别普通路径与断开的符号链接。"""

    return os.path.lexists(path)


def _is_link_like(path: Path) -> bool:
    """拒绝符号链接、junction 与其他重解析点。"""

    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None and is_junction():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _require_plain_file(path: Path, package_root: Path) -> None:
    """确认文件普通、无链接且解析后仍位于 package 内。"""

    if _is_link_like(path):
        raise LocalSkillError(
            "symlink_not_allowed",
            f"runtime path must not be a symlink or reparse point: {path.name}",
        )
    try:
        file_stat = path.lstat()
    except OSError as exc:
        raise LocalSkillError(
            "runtime_file_unreadable",
            f"runtime file cannot be inspected: {path.name}",
        ) from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise LocalSkillError(
            "runtime_file_not_regular",
            f"runtime path is not a regular file: {path.name}",
        )
    try:
        path.resolve(strict=True).relative_to(package_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise LocalSkillError(
            "runtime_path_escape",
            f"runtime file escapes the package: {path.name}",
        ) from exc


def _validate_relative_path(relative_path: str) -> PurePosixPath:
    """验证清单路径为 package 内的规范相对路径。"""

    if not relative_path or "\\" in relative_path:
        raise LocalSkillError(
            "invalid_runtime_path",
            "runtime paths must be non-empty POSIX-style relative paths",
        )
    path = PurePosixPath(relative_path)
    if path.is_absolute() or WINDOWS_ABSOLUTE_RE.match(relative_path):
        raise LocalSkillError(
            "absolute_runtime_path",
            f"absolute runtime path is not allowed: {relative_path}",
        )
    if any(part in {"", ".", ".."} for part in path.parts):
        raise LocalSkillError(
            "runtime_path_escape",
            f"runtime path contains an unsafe segment: {relative_path}",
        )
    return path


def _is_forbidden_artifact(relative_path: PurePosixPath) -> bool:
    """识别不允许进入运行时副本的测试、缓存和临时产物。"""

    lowered_parts = tuple(part.casefold() for part in relative_path.parts)
    if any(part in FORBIDDEN_RUNTIME_PARTS for part in lowered_parts):
        return True
    if any(part.startswith((".tmp-", "pytest-")) for part in lowered_parts):
        return True
    filename = lowered_parts[-1]
    if filename in FORBIDDEN_RUNTIME_FILES:
        return True
    if filename.endswith((".pyc", ".pyo")):
        return True
    if filename.startswith("test_") and filename.endswith(".py"):
        return True
    if filename.endswith("_test.py"):
        return True
    return filename.endswith(".xml") and "junit" in filename


def _split_frontmatter(text: str) -> tuple[str, str]:
    """按当前 Skill frontmatter 边界提取 YAML 与正文。"""

    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise LocalSkillError(
            "frontmatter_missing",
            "SKILL.md must begin with YAML frontmatter",
        )
    frontmatter_lines: list[str] = []
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "".join(frontmatter_lines), "".join(lines[index + 1 :])
        frontmatter_lines.append(line)
    raise LocalSkillError(
        "frontmatter_unterminated",
        "SKILL.md frontmatter is not terminated",
    )


def _validate_frontmatter(skill_file: Path) -> str:
    """验证 UTF-8、YAML、字段白名单与固定 Skill 名称。"""

    try:
        raw_bytes = skill_file.read_bytes()
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LocalSkillError(
            "skill_not_utf8",
            "SKILL.md must be valid UTF-8",
        ) from exc
    except OSError as exc:
        raise LocalSkillError(
            "skill_unreadable",
            "SKILL.md could not be read",
        ) from exc

    frontmatter_text, _ = _split_frontmatter(text)
    try:
        metadata = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        raise LocalSkillError(
            "frontmatter_invalid",
            "SKILL.md frontmatter is not valid YAML",
        ) from exc
    if not isinstance(metadata, dict):
        raise LocalSkillError(
            "frontmatter_invalid",
            "SKILL.md frontmatter top level must be a mapping",
        )
    if any(not isinstance(key, str) for key in metadata):
        raise LocalSkillError(
            "frontmatter_invalid",
            "SKILL.md frontmatter keys must be strings",
        )
    unsupported = sorted(set(metadata) - ALLOWED_FRONTMATTER_FIELDS)
    if unsupported:
        raise LocalSkillError(
            "frontmatter_unsupported_fields",
            "SKILL.md frontmatter contains unsupported top-level fields: "
            + ", ".join(unsupported),
        )
    if metadata.get("name") != SKILL_NAME:
        raise LocalSkillError(
            "frontmatter_name_mismatch",
            f"SKILL.md frontmatter name must be {SKILL_NAME}",
        )
    if "description" in metadata and not isinstance(metadata["description"], str):
        raise LocalSkillError(
            "frontmatter_invalid",
            "SKILL.md frontmatter description must be a string",
        )
    if "metadata" in metadata and not isinstance(metadata["metadata"], dict):
        raise LocalSkillError(
            "frontmatter_invalid",
            "SKILL.md frontmatter metadata must be a mapping",
        )
    return hashlib.sha256(raw_bytes).hexdigest()


def _collect_runtime_files(package_root: Path) -> tuple[Path, ...]:
    """仅从显式白名单收集普通运行时文件。"""

    if _is_link_like(package_root):
        raise LocalSkillError(
            "symlink_not_allowed",
            "runtime package root must not be a symlink or reparse point",
        )
    if not package_root.is_dir():
        raise LocalSkillError(
            "package_missing",
            "runtime package directory does not exist",
        )

    collected: list[Path] = []
    for filename in RUNTIME_ROOT_FILES:
        path = package_root / filename
        if not _lexists(path):
            raise LocalSkillError(
                "skill_missing",
                f"required runtime file is missing: {filename}",
            )
        _require_plain_file(path, package_root)
        collected.append(path)

    for directory_name in RUNTIME_DIRS:
        runtime_dir = package_root / directory_name
        if not _lexists(runtime_dir):
            continue
        if _is_link_like(runtime_dir):
            raise LocalSkillError(
                "symlink_not_allowed",
                f"runtime directory must not be a symlink: {directory_name}",
            )
        if not runtime_dir.is_dir():
            raise LocalSkillError(
                "runtime_directory_invalid",
                f"runtime whitelist entry is not a directory: {directory_name}",
            )
        for current_root, directory_names, filenames in os.walk(
            runtime_dir,
            topdown=True,
            followlinks=False,
        ):
            current = Path(current_root)
            directory_names.sort()
            filenames.sort()
            for child_name in directory_names:
                child = current / child_name
                relative = PurePosixPath(child.relative_to(package_root).as_posix())
                if _is_forbidden_artifact(relative):
                    raise LocalSkillError(
                        "forbidden_runtime_artifact",
                        f"runtime directory contains a forbidden artifact: {relative}",
                    )
                if _is_link_like(child):
                    raise LocalSkillError(
                        "symlink_not_allowed",
                        f"runtime directory contains a symlink: {relative}",
                    )
                if not child.is_dir():
                    raise LocalSkillError(
                        "runtime_directory_invalid",
                        f"runtime path is not a directory: {relative}",
                    )
            for filename in filenames:
                path = current / filename
                relative = PurePosixPath(path.relative_to(package_root).as_posix())
                if _is_forbidden_artifact(relative):
                    raise LocalSkillError(
                        "forbidden_runtime_artifact",
                        f"runtime directory contains a forbidden artifact: {relative}",
                    )
                _require_plain_file(path, package_root)
                collected.append(path)

    return tuple(collected)


def _extract_markdown_destination(raw_target: str) -> str:
    """提取 Markdown 链接目标并忽略可选标题。"""

    target = raw_target.strip()
    if target.startswith("<"):
        closing = target.find(">")
        if closing == -1:
            return target
        return target[1:closing]
    return target.split(maxsplit=1)[0]


def _validate_markdown_links(
    package_root: Path,
    runtime_files: tuple[Path, ...],
) -> None:
    """确认本地 Markdown 引用不逃逸且指向运行时清单。"""

    root_resolved = package_root.resolve(strict=True)
    manifest_paths = {
        path.resolve(strict=True).relative_to(root_resolved)
        for path in runtime_files
    }
    for document in runtime_files:
        if document.suffix.casefold() != ".md":
            continue
        try:
            text = document.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise LocalSkillError(
                "runtime_markdown_unreadable",
                f"runtime Markdown file must be readable UTF-8: {document.name}",
            ) from exc
        for match in MARKDOWN_LINK_RE.finditer(text):
            target = _extract_markdown_destination(match.group("target"))
            if not target or target.startswith("#"):
                continue
            if (
                target.startswith(("/", "\\"))
                or WINDOWS_ABSOLUTE_RE.match(target)
            ):
                raise LocalSkillError(
                    "absolute_reference_path",
                    f"absolute Markdown path is not allowed: {document.name}",
                )
            parsed = urlsplit(target)
            if parsed.scheme:
                if parsed.scheme.casefold() in {"http", "https", "mailto"}:
                    continue
                raise LocalSkillError(
                    "unsupported_reference_scheme",
                    f"unsupported Markdown link scheme: {document.name}",
                )
            local_path = unquote(parsed.path).replace("\\", "/")
            if not local_path:
                continue
            relative = PurePosixPath(local_path)
            if relative.is_absolute() or ".." in relative.parts:
                raise LocalSkillError(
                    "reference_path_escape",
                    f"Markdown link contains an unsafe path: {document.name}",
                )
            candidate = (document.parent / Path(*relative.parts)).resolve(strict=False)
            try:
                candidate_relative = candidate.relative_to(root_resolved)
            except ValueError as exc:
                raise LocalSkillError(
                    "reference_path_escape",
                    f"Markdown link escapes the package: {document.name}",
                ) from exc
            if candidate_relative not in manifest_paths:
                raise LocalSkillError(
                    "reference_not_in_runtime_package",
                    f"Markdown link target is not a runtime file: {document.name}",
                )


def _file_sha256(path: Path) -> str:
    """以流式读取计算文件 SHA-256。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise LocalSkillError(
            "runtime_file_unreadable",
            f"runtime file could not be hashed: {path.name}",
        ) from exc
    return digest.hexdigest()


def _snapshot_package(package_root: Path) -> PackageSnapshot:
    """验证 package 并计算 Skill 与完整运行时版本。"""

    runtime_paths = _collect_runtime_files(package_root)
    _validate_markdown_links(package_root, runtime_paths)
    files: list[RuntimeFile] = []
    for absolute_path in runtime_paths:
        relative_path = absolute_path.relative_to(package_root).as_posix()
        _validate_relative_path(relative_path)
        files.append(
            RuntimeFile(
                relative_path=relative_path,
                absolute_path=absolute_path,
                sha256=_file_sha256(absolute_path),
            )
        )
    files.sort(key=lambda item: item.relative_path)
    skill_revision = _validate_frontmatter(package_root / "SKILL.md")

    package_digest = hashlib.sha256()
    for runtime_file in files:
        package_digest.update(runtime_file.relative_path.encode("utf-8"))
        package_digest.update(b"\0")
        package_digest.update(runtime_file.sha256.encode("ascii"))
        package_digest.update(b"\n")
    return PackageSnapshot(
        files=tuple(files),
        skill_revision=skill_revision,
        package_revision=package_digest.hexdigest(),
    )


def _skills_root(value: str | None) -> Path:
    """解析显式目录或当前 Hermes 配置中的用户 Skill 根目录。"""

    raw_root = Path(value).expanduser() if value else hermes_home() / "skills"
    try:
        root = raw_root.resolve(strict=False)
    except OSError as exc:
        raise LocalSkillError(
            "skills_root_invalid",
            "skills root could not be resolved",
        ) from exc
    if _lexists(root) and not root.is_dir():
        raise LocalSkillError(
            "skills_root_invalid",
            "skills root must be a directory",
        )
    if root.parent == root:
        raise LocalSkillError(
            "skills_root_invalid",
            "filesystem root is not a valid skills root",
        )
    return root


def _target_path(skills_root: Path) -> Path:
    """构造并验证固定的直接子目录目标。"""

    target = skills_root / SKILL_NAME
    if target == skills_root or target.name != SKILL_NAME or target.parent != skills_root:
        raise LocalSkillError(
            "unsafe_target",
            "target must be the claude-code direct child of skills root",
        )
    target_resolved = target.resolve(strict=False)
    source_resolved = SOURCE_DIR.resolve(strict=True)
    if (
        target_resolved == source_resolved
        or target_resolved.is_relative_to(source_resolved)
        or source_resolved.is_relative_to(target_resolved)
    ):
        raise LocalSkillError(
            "unsafe_target",
            "installed target must not overlap the isolated development source",
        )
    if target_resolved.is_relative_to(BUNDLED_SKILLS_ROOT):
        raise LocalSkillError(
            "unsafe_target",
            "bundled skills directory is not a valid local installation root",
        )
    if target_resolved.parent != skills_root:
        raise LocalSkillError(
            "unsafe_target",
            "target escapes skills root or is not a direct child",
        )
    return target


def _copy_runtime_files(snapshot: PackageSnapshot, staging: Path) -> None:
    """逐项复制清单，不复制开发、测试或治理文件。"""

    for runtime_file in snapshot.files:
        relative = _validate_relative_path(runtime_file.relative_path)
        _require_plain_file(runtime_file.absolute_path, SOURCE_DIR)
        destination = staging.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(runtime_file.absolute_path, destination)
        except OSError as exc:
            raise LocalSkillError(
                "copy_failed",
                f"runtime file could not be copied: {runtime_file.relative_path}",
            ) from exc


def _validate_staging_layout(staging: Path) -> None:
    """确认 staging 顶层只包含明确允许的运行时入口。"""

    allowed_names = set(RUNTIME_ROOT_FILES) | set(RUNTIME_DIRS)
    try:
        entries = tuple(staging.iterdir())
    except OSError as exc:
        raise LocalSkillError(
            "staging_validation_failed",
            "staging directory could not be inspected",
        ) from exc
    for entry in entries:
        if entry.name not in allowed_names or _is_link_like(entry):
            raise LocalSkillError(
                "staging_validation_failed",
                "staging contains a path outside the runtime whitelist",
            )
        if entry.name in RUNTIME_ROOT_FILES and not entry.is_file():
            raise LocalSkillError(
                "staging_validation_failed",
                "staging runtime root entry is not a regular file",
            )
        if entry.name in RUNTIME_DIRS and not entry.is_dir():
            raise LocalSkillError(
                "staging_validation_failed",
                "staging runtime directory entry is not a directory",
            )


def _remove_staging(staging: Path | None, skills_root: Path) -> None:
    """仅清理本次在 skills root 下创建的 staging 目录。"""

    if staging is None or not _lexists(staging):
        return
    if (
        staging.parent != skills_root
        or not staging.name.startswith(STAGING_PREFIX)
        or _is_link_like(staging)
    ):
        raise LocalSkillError(
            "staging_cleanup_failed",
            "staging path failed cleanup safety validation",
        )
    try:
        shutil.rmtree(staging)
    except OSError as exc:
        raise LocalSkillError(
            "staging_cleanup_failed",
            "staging directory could not be removed",
        ) from exc
    if _lexists(staging):
        raise LocalSkillError(
            "staging_cleanup_failed",
            "staging directory still exists after cleanup",
        )


def install(skills_dir: str | None) -> dict:
    """验证、暂存并原子发布本地用户 Skill。"""

    source_snapshot = _snapshot_package(SOURCE_DIR)
    skills_root = _skills_root(skills_dir)
    target = _target_path(skills_root)
    if _lexists(target):
        raise LocalSkillError(
            "target_exists",
            "target already exists; run uninstall before installing again",
        )
    try:
        skills_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LocalSkillError(
            "skills_root_create_failed",
            "skills root could not be created",
        ) from exc

    staging: Path | None = None
    try:
        staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=skills_root))
        _copy_runtime_files(source_snapshot, staging)
        _validate_staging_layout(staging)
        staged_snapshot = _snapshot_package(staging)
        if (
            staged_snapshot.package_revision != source_snapshot.package_revision
            or staged_snapshot.skill_revision != source_snapshot.skill_revision
            or tuple(item.relative_path for item in staged_snapshot.files)
            != tuple(item.relative_path for item in source_snapshot.files)
        ):
            raise LocalSkillError(
                "staging_validation_failed",
                "staged runtime package does not match the validated source",
            )
        if _lexists(target):
            raise LocalSkillError(
                "target_exists",
                "target appeared during installation; staging was not published",
            )
        try:
            os.rename(staging, target)
        except OSError as exc:
            raise LocalSkillError(
                "publish_failed",
                "staged runtime package could not be published atomically",
            ) from exc
        staging = None
    finally:
        _remove_staging(staging, skills_root)

    return {
        "ok": True,
        "action": "install",
        "installed_path": str(target),
        "file_count": len(source_snapshot.files),
        "skill_sha256": source_snapshot.skill_revision,
        "skill_revision": source_snapshot.skill_revision,
        "package_revision": source_snapshot.package_revision,
    }


def _snapshot_status(package_root: Path) -> tuple[PackageSnapshot | None, dict | None]:
    """为只读 status 捕获验证错误而不修改目录。"""

    try:
        return _snapshot_package(package_root), None
    except LocalSkillError as exc:
        return None, {
            "error_type": exc.error_type,
            "error": exc.message,
        }


def status(skills_dir: str | None) -> dict:
    """只读比较开发源与已安装运行时 package。"""

    skills_root = _skills_root(skills_dir)
    target = _target_path(skills_root)
    source_exists = _lexists(SOURCE_DIR / "SKILL.md")
    installed = _lexists(target)
    source_snapshot, source_error = (
        _snapshot_status(SOURCE_DIR) if source_exists else (None, None)
    )
    installed_snapshot: PackageSnapshot | None = None
    installed_error: dict | None = None
    if installed:
        if _is_link_like(target):
            installed_error = {
                "error_type": "unsafe_target",
                "error": "installed target must not be a symlink or reparse point",
            }
        elif not target.is_dir():
            installed_error = {
                "error_type": "installed_target_invalid",
                "error": "installed target must be a directory",
            }
        else:
            installed_snapshot, installed_error = _snapshot_status(target)

    result = {
        "ok": source_snapshot is not None and installed_error is None,
        "action": "status",
        "source_exists": source_exists,
        "installed": installed,
        "source_revision": (
            source_snapshot.skill_revision if source_snapshot is not None else None
        ),
        "installed_revision": (
            installed_snapshot.skill_revision
            if installed_snapshot is not None
            else None
        ),
        "source_package_revision": (
            source_snapshot.package_revision if source_snapshot is not None else None
        ),
        "installed_package_revision": (
            installed_snapshot.package_revision
            if installed_snapshot is not None
            else None
        ),
        "in_sync": bool(
            source_snapshot is not None
            and installed_snapshot is not None
            and source_snapshot.package_revision
            == installed_snapshot.package_revision
        ),
        "installed_path": str(target),
    }
    if source_error is not None:
        result["source_error"] = source_error
    if installed_error is not None:
        result["installed_error"] = installed_error
    return result


def _assert_no_links_recursive(target: Path) -> None:
    """卸载前拒绝目标树内的链接与重解析点。"""

    for current_root, directory_names, filenames in os.walk(
        target,
        topdown=True,
        followlinks=False,
    ):
        current = Path(current_root)
        for name in (*directory_names, *filenames):
            child = current / name
            if _is_link_like(child):
                raise LocalSkillError(
                    "unsafe_uninstall_tree",
                    "installed target contains a symlink or reparse point",
                )


def uninstall(skills_dir: str | None) -> dict:
    """仅删除固定的本地 claude-code Skill 直接子目录。"""

    skills_root = _skills_root(skills_dir)
    target = _target_path(skills_root)
    if not _lexists(target):
        return {
            "ok": True,
            "action": "uninstall",
            "already_absent": True,
            "installed_path": str(target),
        }
    if _is_link_like(target):
        raise LocalSkillError(
            "unsafe_target",
            "uninstall refuses a symlink or reparse-point target",
        )
    if not target.is_dir():
        raise LocalSkillError(
            "unsafe_target",
            "uninstall target must be a directory",
        )
    try:
        target_resolved = target.resolve(strict=True)
    except OSError as exc:
        raise LocalSkillError(
            "unsafe_target",
            "uninstall target could not be resolved",
        ) from exc
    if (
        target_resolved == skills_root
        or target_resolved.parent != skills_root
        or target_resolved.name != SKILL_NAME
        or target_resolved == SOURCE_DIR.resolve(strict=True)
    ):
        raise LocalSkillError(
            "unsafe_target",
            "uninstall target is not the expected direct child of skills root",
        )
    _assert_no_links_recursive(target_resolved)
    try:
        shutil.rmtree(target_resolved)
    except OSError as exc:
        raise LocalSkillError(
            "uninstall_failed",
            "installed target could not be removed completely",
        ) from exc
    if _lexists(target_resolved):
        raise LocalSkillError(
            "uninstall_failed",
            "installed target still exists after uninstall",
        )
    return {
        "ok": True,
        "action": "uninstall",
        "already_absent": False,
        "installed_path": str(target_resolved),
    }


def _build_parser() -> argparse.ArgumentParser:
    """构建三个显式子命令及隔离目录覆盖参数。"""

    parser = argparse.ArgumentParser(
        description="Manage the isolated Claude Code Skill as a local user Skill."
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("install", "status", "uninstall"):
        subparser = subparsers.add_parser(action)
        subparser.add_argument(
            "--skills-dir",
            help="Override the user skills root for a later isolated validation.",
        )
    return parser


def _print_result(result: dict) -> None:
    """只输出结构化摘要，不输出 Skill 正文。"""

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    """分派命令并将失败转换为稳定的结构化结果。"""

    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.action == "install":
            result = install(args.skills_dir)
        elif args.action == "status":
            result = status(args.skills_dir)
        else:
            result = uninstall(args.skills_dir)
    except LocalSkillError as exc:
        _print_result(
            {
                "ok": False,
                "action": args.action,
                "error_type": exc.error_type,
                "error": exc.message,
            }
        )
        return 1
    except OSError:
        _print_result(
            {
                "ok": False,
                "action": args.action,
                "error_type": "filesystem_error",
                "error": "filesystem operation failed",
            }
        )
        return 1
    _print_result(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
