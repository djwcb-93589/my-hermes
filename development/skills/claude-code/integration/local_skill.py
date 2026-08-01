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
from datetime import datetime, timezone
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
INSTALLER_STATE_DIR = ".installer-state"
INSTALLER_ID = "myhermes-claude-code-local-skill"
OWNERSHIP_SCHEMA_VERSION = 1
OWNERSHIP_MAX_BYTES = 64 * 1024
GOVERNANCE_FILE = ".myhermes.json"
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\((?P<target>[^)\n]+)\)")
WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class LocalSkillError(Exception):
    """表示可稳定报告给调用者的本地 Skill 操作错误。"""

    def __init__(
        self,
        error_type: str,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ):
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.details = dict(details or {})


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


@dataclass(frozen=True)
class OwnershipState:
    """表示安装器所有权记录的只读验证结果。"""

    exists: bool
    valid: bool
    managed_by_installer: bool
    record: dict | None = None
    fingerprint: str | None = None
    error_type: str | None = None
    error: str | None = None


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
io_utils = importlib.import_module("hermes._io_utils")
atomic_write_text = io_utils.atomic_write_text
LockTimeout = io_utils.LockTimeout
acquire_skill_lock = importlib.import_module(
    "hermes.skill_locking"
).acquire_skill_lock
trust_store = importlib.import_module("hermes.skill_trust")
acquire_trust_store_lock = trust_store.acquire_trust_store_lock
inspect_skill_trust_state = trust_store.inspect_skill_trust_state
SkillTrustStoreState = trust_store.SkillTrustStoreState


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
    except (OSError, RuntimeError) as exc:
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
    """按规范目标确认本地 Markdown 引用留在运行时清单内。"""

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
            if (
                local_path.startswith("/")
                or WINDOWS_ABSOLUTE_RE.match(local_path)
            ):
                raise LocalSkillError(
                    "absolute_reference_path",
                    f"absolute Markdown path is not allowed: {document.name}",
                )
            relative = PurePosixPath(local_path)
            if relative.is_absolute():
                raise LocalSkillError(
                    "absolute_reference_path",
                    f"absolute Markdown path is not allowed: {document.name}",
                )

            # 允许包内 ``..``，但拒绝原始解析路径经过任何链接或重解析点。
            current = document.parent
            for part in relative.parts:
                if part == "..":
                    current = current.parent
                    continue
                current = current / part
                if _lexists(current) and _is_link_like(current):
                    raise LocalSkillError(
                        "reference_path_escape",
                        f"Markdown link traverses a linked path: {document.name}",
                    )

            try:
                candidate = (
                    document.parent / Path(*relative.parts)
                ).resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                raise LocalSkillError(
                    "reference_path_escape",
                    f"Markdown link could not be resolved safely: {document.name}",
                ) from exc
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
    except (OSError, RuntimeError) as exc:
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


def _direct_target_path(skills_root: Path) -> Path:
    """构造固定的直接子目录目标，不解析现有路径。"""

    target = skills_root / SKILL_NAME
    if target == skills_root or target.name != SKILL_NAME or target.parent != skills_root:
        raise LocalSkillError(
            "unsafe_target",
            "target must be the claude-code direct child of skills root",
        )
    return target


def _target_path(skills_root: Path) -> Path:
    """构造并验证固定的直接子目录目标。"""

    target = _direct_target_path(skills_root)
    try:
        target_resolved = target.resolve(strict=False)
        source_resolved = SOURCE_DIR.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LocalSkillError(
            "unsafe_target",
            "target or development source could not be resolved safely",
        ) from exc
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


def _ownership_paths(skills_root: Path) -> tuple[Path, Path]:
    """返回 package 外的状态目录和固定所有权记录路径。"""

    state_dir = skills_root / INSTALLER_STATE_DIR
    state_path = state_dir / f"{SKILL_NAME}.json"
    if state_dir.parent != skills_root or state_path.parent != state_dir:
        raise LocalSkillError(
            "unsafe_target",
            "installer state must remain a direct child of skills root",
        )
    try:
        if _lexists(state_dir):
            if _is_link_like(state_dir) or not state_dir.is_dir():
                raise LocalSkillError(
                    "ownership_record_invalid",
                    "installer state directory is not a plain directory",
                )
            state_dir_resolved = state_dir.resolve(strict=True)
        else:
            state_dir_resolved = state_dir.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise LocalSkillError(
            "ownership_record_invalid",
            "installer state directory could not be resolved safely",
        ) from exc
    if state_dir_resolved.parent != skills_root:
        raise LocalSkillError(
            "unsafe_target",
            "installer state directory escapes skills root",
        )
    if _lexists(state_path) and _is_link_like(state_path):
        raise LocalSkillError(
            "ownership_record_invalid",
            "ownership record must not be a symlink or reparse point",
        )
    return state_dir, state_path


def _ownership_failure(
    *,
    exists: bool,
    error_type: str,
    error: str,
    fingerprint: str | None = None,
    managed_by_installer: bool = False,
) -> OwnershipState:
    """构造不泄露记录正文的所有权验证失败。"""

    return OwnershipState(
        exists=exists,
        valid=False,
        managed_by_installer=managed_by_installer,
        fingerprint=fingerprint,
        error_type=error_type,
        error=error,
    )


def _parse_installed_at(value: object) -> bool:
    """验证项目现有的 UTC ISO-8601 时间格式。"""

    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _read_ownership_state(skills_root: Path, target: Path) -> OwnershipState:
    """只读并严格验证安装器所有权记录。"""

    try:
        _, state_path = _ownership_paths(skills_root)
    except LocalSkillError as exc:
        return _ownership_failure(
            exists=True,
            error_type=exc.error_type,
            error=exc.message,
        )
    if not _lexists(state_path):
        return OwnershipState(
            exists=False,
            valid=False,
            managed_by_installer=False,
        )
    if not state_path.is_file():
        return _ownership_failure(
            exists=True,
            error_type="ownership_record_invalid",
            error="ownership record is not a regular file",
        )
    try:
        file_stat = state_path.lstat()
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > OWNERSHIP_MAX_BYTES:
            return _ownership_failure(
                exists=True,
                error_type="ownership_record_invalid",
                error="ownership record is not a bounded regular file",
            )
        raw_bytes = state_path.read_bytes()
        if len(raw_bytes) > OWNERSHIP_MAX_BYTES:
            return _ownership_failure(
                exists=True,
                error_type="ownership_record_invalid",
                error="ownership record exceeds the size limit",
            )
        raw_text = raw_bytes.decode("utf-8")
        data = json.loads(raw_text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _ownership_failure(
            exists=True,
            error_type="ownership_record_invalid",
            error="ownership record is unreadable or invalid JSON",
        )
    fingerprint = hashlib.sha256(raw_bytes).hexdigest()
    if not isinstance(data, dict):
        return _ownership_failure(
            exists=True,
            error_type="ownership_record_invalid",
            error="ownership record must be a JSON object",
            fingerprint=fingerprint,
        )

    current_identity = (
        data.get("schema_version") == OWNERSHIP_SCHEMA_VERSION
        and data.get("installer_id") == INSTALLER_ID
        and data.get("skill_name") == SKILL_NAME
    )
    if data.get("schema_version") != OWNERSHIP_SCHEMA_VERSION:
        return _ownership_failure(
            exists=True,
            error_type="unsupported_state_version",
            error="ownership record schema version is unsupported",
            fingerprint=fingerprint,
        )
    if data.get("installer_id") != INSTALLER_ID or data.get("skill_name") != SKILL_NAME:
        return _ownership_failure(
            exists=True,
            error_type="ownership_mismatch",
            error="ownership record belongs to a different installer or Skill",
            fingerprint=fingerprint,
        )

    target_value = data.get("target_path")
    if not isinstance(target_value, str) or not target_value:
        return _ownership_failure(
            exists=True,
            error_type="ownership_record_invalid",
            error="ownership record target_path is missing or invalid",
            fingerprint=fingerprint,
            managed_by_installer=current_identity,
        )
    record_target = Path(target_value)
    if not record_target.is_absolute():
        return _ownership_failure(
            exists=True,
            error_type="ownership_record_invalid",
            error="ownership record target_path must be absolute",
            fingerprint=fingerprint,
            managed_by_installer=current_identity,
        )
    try:
        canonical_target = target.resolve(strict=False)
        canonical_record_target = record_target.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return _ownership_failure(
            exists=True,
            error_type="ownership_record_invalid",
            error="ownership record target_path cannot be resolved",
            fingerprint=fingerprint,
            managed_by_installer=current_identity,
        )
    if (
        canonical_record_target != canonical_target
        or target_value != str(canonical_target)
    ):
        return _ownership_failure(
            exists=True,
            error_type="ownership_mismatch",
            error="ownership record target_path does not match the current target",
            fingerprint=fingerprint,
            managed_by_installer=current_identity,
        )

    package_revision = data.get("package_revision")
    installed_revision = data.get("installed_revision")
    if (
        not isinstance(package_revision, str)
        or not SHA256_RE.fullmatch(package_revision)
        or not isinstance(installed_revision, str)
        or not SHA256_RE.fullmatch(installed_revision)
        or package_revision != installed_revision
        or not _parse_installed_at(data.get("installed_at"))
    ):
        return _ownership_failure(
            exists=True,
            error_type="ownership_record_invalid",
            error="ownership record revisions or installed_at are invalid",
            fingerprint=fingerprint,
            managed_by_installer=current_identity,
        )
    return OwnershipState(
        exists=True,
        valid=True,
        managed_by_installer=True,
        record=dict(data),
        fingerprint=fingerprint,
    )


def _ownership_payload(
    target: Path,
    source_snapshot: PackageSnapshot,
    installed_snapshot: PackageSnapshot,
) -> str:
    """生成不包含凭据和 Skill 正文的所有权记录。"""

    record = {
        "schema_version": OWNERSHIP_SCHEMA_VERSION,
        "installer_id": INSTALLER_ID,
        "skill_name": SKILL_NAME,
        "target_path": str(target.resolve(strict=True)),
        "package_revision": source_snapshot.package_revision,
        "installed_revision": installed_snapshot.package_revision,
        "installed_at": datetime.now(timezone.utc).isoformat().replace(
            "+00:00",
            "Z",
        ),
    }
    return json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _write_ownership_record(
    skills_root: Path,
    target: Path,
    payload: str,
) -> OwnershipState:
    """原子写入并重新读取所有权记录。"""

    state_dir, state_path = _ownership_paths(skills_root)
    if _lexists(state_path):
        raise LocalSkillError(
            "ownership_mismatch",
            "ownership record appeared during installation",
        )
    try:
        state_dir.mkdir(parents=False, exist_ok=True)
    except OSError as exc:
        raise LocalSkillError(
            "state_write_failed",
            "installer state directory could not be created",
        ) from exc
    state_dir, state_path = _ownership_paths(skills_root)
    temporary_path = state_path.with_suffix(state_path.suffix + ".tmp")
    if _lexists(temporary_path):
        raise LocalSkillError(
            "state_write_failed",
            "ownership record temporary path already exists",
        )
    try:
        atomic_write_text(state_path, payload)
    except (OSError, UnicodeError) as exc:
        raise LocalSkillError(
            "state_write_failed",
            "ownership record could not be written atomically",
        ) from exc
    state = _read_ownership_state(skills_root, target)
    if not state.valid:
        raise LocalSkillError(
            "state_write_failed",
            "ownership record could not be verified after writing",
        )
    return state


def _cleanup_empty_state_dir(skills_root: Path) -> None:
    """仅在安装器状态目录为空时将其移除。"""

    state_dir, _ = _ownership_paths(skills_root)
    if not state_dir.exists():
        return
    try:
        if next(state_dir.iterdir(), None) is not None:
            return
        state_dir.rmdir()
    except FileNotFoundError:
        return
    except OSError as exc:
        try:
            if state_dir.is_dir() and next(state_dir.iterdir(), None) is not None:
                return
        except OSError:
            pass
        raise LocalSkillError(
            "state_cleanup_failed",
            "empty installer state directory could not be removed",
            details={"ownership_removed": True},
        ) from exc


def _governance_reason(target: Path) -> str | None:
    """保守识别现有治理 sidecar，不复制治理授权规则。"""

    governance_path = target / GOVERNANCE_FILE
    if _lexists(governance_path):
        return "skill_managed"
    return None


def _trust_block_reason(trust_state: SkillTrustStoreState) -> str | None:
    """把通用 trust 只读状态映射为安装器的保守阻塞原因。"""

    if not trust_state.reliable:
        return trust_state.error_type or "trust_state_unknown"
    if trust_state.record_present:
        return "skill_managed"
    return None


def _management_block_reason(target: Path) -> str | None:
    """组合治理 sidecar 与通用 trust 状态，不读取存储格式。"""

    governance_reason = _governance_reason(target)
    if governance_reason is not None:
        return governance_reason
    return _trust_block_reason(inspect_skill_trust_state(SKILL_NAME))


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


def _validate_package_layout(
    package_root: Path,
    *,
    allow_governance: bool,
    error_type: str,
) -> None:
    """确认已发布或 staging 顶层没有清单外内容。"""

    allowed_names = set(RUNTIME_ROOT_FILES) | set(RUNTIME_DIRS)
    if allow_governance:
        allowed_names.add(GOVERNANCE_FILE)
    try:
        entries = tuple(package_root.iterdir())
    except OSError as exc:
        raise LocalSkillError(
            error_type,
            "runtime package directory could not be inspected",
        ) from exc
    for entry in entries:
        if entry.name not in allowed_names or _is_link_like(entry):
            raise LocalSkillError(
                error_type,
                "runtime package contains a path outside the allowed layout",
            )
        if entry.name == GOVERNANCE_FILE:
            if not entry.is_file():
                raise LocalSkillError(
                    error_type,
                    "governance sidecar is not a regular file",
                )
            continue
        if entry.name in RUNTIME_ROOT_FILES and not entry.is_file():
            raise LocalSkillError(
                error_type,
                "runtime package root entry is not a regular file",
            )
        if entry.name in RUNTIME_DIRS and not entry.is_dir():
            raise LocalSkillError(
                error_type,
                "runtime package directory entry is not a directory",
            )


def _validate_staging_layout(staging: Path) -> None:
    """确认 staging 只包含明确允许的运行时入口。"""

    _validate_package_layout(
        staging,
        allow_governance=False,
        error_type="staging_validation_failed",
    )


def _snapshot_installed_package(target: Path) -> PackageSnapshot:
    """对安装副本执行严格布局校验并计算 revision。"""

    _validate_package_layout(
        target,
        allow_governance=True,
        error_type="target_changed",
    )
    return _snapshot_package(target)


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


def _rollback_published_install_checked(
    skills_root: Path,
    target: Path,
    *,
    expected_revision: str,
    expected_state_fingerprint: str,
) -> None:
    """仅在目录和可选状态仍属于本次发布时执行回滚。"""

    _, state_path = _ownership_paths(skills_root)
    if _lexists(state_path):
        if _is_link_like(state_path) or not state_path.is_file():
            raise LocalSkillError(
                "rollback_failed",
                "ownership record changed before rollback",
            )
        try:
            state_stat = state_path.lstat()
            if (
                not stat.S_ISREG(state_stat.st_mode)
                or state_stat.st_size > OWNERSHIP_MAX_BYTES
            ):
                raise LocalSkillError(
                    "rollback_failed",
                    "ownership record is unsafe to read during rollback",
                )
            state_bytes = state_path.read_bytes()
            if len(state_bytes) > OWNERSHIP_MAX_BYTES:
                raise LocalSkillError(
                    "rollback_failed",
                    "ownership record changed size during rollback",
                )
            state_fingerprint = hashlib.sha256(state_bytes).hexdigest()
        except OSError as exc:
            raise LocalSkillError(
                "rollback_failed",
                "ownership record could not be verified for rollback",
            ) from exc
        if state_fingerprint != expected_state_fingerprint:
            raise LocalSkillError(
                "rollback_failed",
                "ownership record changed before rollback",
            )

    if _lexists(target):
        if _is_link_like(target) or not target.is_dir():
            raise LocalSkillError(
                "rollback_failed",
                "published target is no longer a plain directory",
            )
        if _management_block_reason(target) is not None:
            raise LocalSkillError(
                "rollback_failed",
                "published target entered managed state before rollback",
            )
        try:
            current_snapshot = _snapshot_installed_package(target)
        except LocalSkillError as exc:
            raise LocalSkillError(
                "rollback_failed",
                "published target could not be verified for rollback",
            ) from exc
        if current_snapshot.package_revision != expected_revision:
            raise LocalSkillError(
                "rollback_failed",
                "published target changed before rollback",
            )
        _assert_no_links_recursive(target)
        try:
            shutil.rmtree(target)
        except OSError as exc:
            raise LocalSkillError(
                "rollback_failed",
                "published target could not be removed during rollback",
            ) from exc
        if _lexists(target):
            raise LocalSkillError(
                "rollback_failed",
                "published target still exists after rollback",
            )

    if _lexists(state_path):
        if _lexists(target):
            raise LocalSkillError(
                "rollback_failed",
                "published target reappeared before ownership rollback",
            )
        try:
            state_path.unlink()
        except OSError as exc:
            raise LocalSkillError(
                "rollback_failed",
                "ownership record could not be removed during rollback",
            ) from exc
    _cleanup_empty_state_dir(skills_root)


def _rollback_published_install(
    skills_root: Path,
    target: Path,
    *,
    expected_revision: str,
    expected_state_fingerprint: str,
) -> None:
    """将任何无法安全完成的回滚统一报告为 rollback_failed。"""

    try:
        _rollback_published_install_checked(
            skills_root,
            target,
            expected_revision=expected_revision,
            expected_state_fingerprint=expected_state_fingerprint,
        )
    except LocalSkillError as exc:
        if exc.error_type == "rollback_failed":
            raise
        raise LocalSkillError(
            "rollback_failed",
            "published target could not be rolled back safely",
        ) from exc
    except (OSError, TypeError, ValueError) as exc:
        raise LocalSkillError(
            "rollback_failed",
            "published target could not be rolled back safely",
        ) from exc


def install(skills_dir: str | None) -> dict:
    """在统一 Skill 锁内发布 package 和外部所有权记录。"""

    skills_root = _skills_root(skills_dir)
    try:
        skills_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LocalSkillError(
            "skills_root_create_failed",
            "skills root could not be created",
        ) from exc

    try:
        with acquire_skill_lock(skills_root, SKILL_NAME):
            locked_root = _skills_root(skills_dir)
            if locked_root != skills_root:
                raise LocalSkillError(
                    "unsafe_target",
                    "skills root changed while acquiring the Skill lock",
                )
            target = _target_path(locked_root)
            ownership = _read_ownership_state(locked_root, target)
            if _lexists(target):
                raise LocalSkillError(
                    "target_exists",
                    "target already exists; run uninstall before installing again",
                )
            if ownership.exists:
                raise LocalSkillError(
                    ownership.error_type or "ownership_mismatch",
                    ownership.error
                    or "ownership record already exists for an absent target",
                )
            management_reason = _management_block_reason(target)
            if management_reason is not None:
                raise LocalSkillError(
                    management_reason,
                    "existing Skill management state must be resolved before install",
                )

            source_snapshot = _snapshot_package(SOURCE_DIR)
            staging: Path | None = None
            try:
                staging = Path(
                    tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=locked_root)
                )
                _copy_runtime_files(source_snapshot, staging)
                _validate_staging_layout(staging)
                staged_snapshot = _snapshot_package(staging)
                if (
                    staged_snapshot.package_revision
                    != source_snapshot.package_revision
                    or staged_snapshot.skill_revision
                    != source_snapshot.skill_revision
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
                current_ownership = _read_ownership_state(locked_root, target)
                if current_ownership.exists:
                    raise LocalSkillError(
                        current_ownership.error_type or "ownership_mismatch",
                        current_ownership.error
                        or "ownership record appeared during installation",
                    )
                management_reason = _management_block_reason(target)
                if management_reason is not None:
                    raise LocalSkillError(
                        management_reason,
                        "Skill management state changed during installation",
                    )
                try:
                    os.rename(staging, target)
                except OSError as exc:
                    raise LocalSkillError(
                        "publish_failed",
                        "staged runtime package could not be published atomically",
                    ) from exc
                staging = None

                state_fingerprint = ""
                failure_type = "publish_failed"
                try:
                    installed_snapshot = _snapshot_installed_package(target)
                    if (
                        installed_snapshot.package_revision
                        != staged_snapshot.package_revision
                    ):
                        raise LocalSkillError(
                            "publish_failed",
                            "published runtime package revision does not match staging",
                        )
                    ownership_payload = _ownership_payload(
                        target,
                        source_snapshot,
                        installed_snapshot,
                    )
                    state_fingerprint = hashlib.sha256(
                        ownership_payload.encode("utf-8")
                    ).hexdigest()
                    failure_type = "state_write_failed"
                    written_state = _write_ownership_record(
                        locked_root,
                        target,
                        ownership_payload,
                    )
                    if written_state.fingerprint != state_fingerprint:
                        raise LocalSkillError(
                            "state_write_failed",
                            "ownership record fingerprint mismatch",
                        )
                except (LocalSkillError, OSError, TypeError, ValueError) as exc:
                    try:
                        _rollback_published_install(
                            locked_root,
                            target,
                            expected_revision=staged_snapshot.package_revision,
                            expected_state_fingerprint=state_fingerprint,
                        )
                    except LocalSkillError as rollback_error:
                        raise rollback_error from exc
                    raise LocalSkillError(
                        failure_type,
                        "published target was rolled back after install finalization failed",
                    ) from exc
            finally:
                _remove_staging(staging, locked_root)

            _, state_path = _ownership_paths(locked_root)
            return {
                "ok": True,
                "action": "install",
                "installed_path": str(target.resolve(strict=True)),
                "ownership_path": str(state_path.resolve(strict=True)),
                "file_count": len(source_snapshot.files),
                "skill_sha256": installed_snapshot.skill_revision,
                "skill_revision": installed_snapshot.skill_revision,
                "package_revision": source_snapshot.package_revision,
                "installed_package_revision": (
                    installed_snapshot.package_revision
                ),
            }
    except LockTimeout as exc:
        raise LocalSkillError(
            "lock_timeout",
            "could not acquire skill operation lock",
        ) from exc


def _snapshot_status(
    package_root: Path,
    *,
    installed: bool = False,
) -> tuple[PackageSnapshot | None, dict | None]:
    """为只读 status 捕获验证错误而不修改目录。"""

    try:
        snapshot = (
            _snapshot_installed_package(package_root)
            if installed
            else _snapshot_package(package_root)
        )
        return snapshot, None
    except LocalSkillError as exc:
        return None, {
            "error_type": exc.error_type,
            "error": exc.message,
        }


def _status_observation(skills_root: Path, target: Path) -> dict:
    """生成一次不写文件的安装、治理与 trust 一致性观察。"""

    target_present = _lexists(target)
    ownership = _read_ownership_state(skills_root, target)
    installed = False
    installed_snapshot: PackageSnapshot | None = None
    installed_error: dict | None = None
    try:
        _target_path(skills_root)
    except LocalSkillError as exc:
        installed_error = {
            "error_type": exc.error_type,
            "error": exc.message,
        }
    if target_present and installed_error is None:
        if _is_link_like(target):
            installed_error = {
                "error_type": "unsafe_target",
                "error": "installed target must not be a symlink or reparse point",
            }
        elif not target.is_dir():
            installed_error = {
                "error_type": "unsafe_target",
                "error": "installed target must be a directory",
            }
        else:
            installed = True
            installed_snapshot, installed_error = _snapshot_status(
                target,
                installed=True,
            )
    governance_reason = _governance_reason(target)
    trust_state = inspect_skill_trust_state(SKILL_NAME)
    managed = bool(
        governance_reason is not None
        or (trust_state.reliable and trust_state.record_present)
    )
    readiness_signature = (
        target_present,
        installed,
        (
            installed_snapshot.skill_revision,
            installed_snapshot.package_revision,
        )
        if installed_snapshot is not None
        else None,
        installed_error.get("error_type") if installed_error else None,
        ownership.exists,
        ownership.valid,
        ownership.fingerprint,
        ownership.error_type,
    )
    signature = (
        readiness_signature,
        governance_reason,
        trust_state.reliable,
        trust_state.record_present,
        trust_state.fingerprint,
        trust_state.error_type,
    )
    return {
        "target_present": target_present,
        "installed": installed,
        "ownership": ownership,
        "installed_snapshot": installed_snapshot,
        "installed_error": installed_error,
        "governance_reason": governance_reason,
        "trust_state": trust_state,
        "managed": managed,
        "readiness_signature": readiness_signature,
        "signature": signature,
    }


def _readiness_reason(observation: dict, *, concurrent_change: bool) -> str | None:
    """只按安装完整性给出未就绪原因，不混入治理或版本同步状态。"""

    if concurrent_change:
        return "concurrent_change"
    ownership: OwnershipState = observation["ownership"]
    installed_error = observation["installed_error"]
    if (
        installed_error is not None
        and installed_error.get("error_type") == "unsafe_target"
    ):
        return "unsafe_target"
    if not observation["target_present"]:
        return "not_installed"
    if not observation["installed"]:
        return (
            installed_error.get("error_type")
            if installed_error is not None
            else "unsafe_target"
        )
    if not ownership.exists:
        return "ownership_record_missing"
    if not ownership.valid:
        return ownership.error_type or "ownership_record_invalid"
    if installed_error is not None:
        error_type = installed_error.get("error_type")
        if error_type in {"unsafe_target", "target_changed"}:
            return error_type
        return "installed_content_invalid"
    installed_snapshot: PackageSnapshot | None = observation["installed_snapshot"]
    if installed_snapshot is None:
        return "installed_content_invalid"
    if (
        installed_snapshot.package_revision
        != ownership.record["installed_revision"]
    ):
        return "target_changed"
    return None


def _uninstall_block_reason(
    observation: dict,
    *,
    concurrent_change: bool,
) -> str | None:
    """独立判断安装器是否仍有权执行自动卸载。"""

    readiness_reason = _readiness_reason(
        observation,
        concurrent_change=concurrent_change,
    )
    if readiness_reason is not None:
        return readiness_reason
    if observation["governance_reason"] is not None:
        return observation["governance_reason"]
    return _trust_block_reason(observation["trust_state"])


def status(skills_dir: str | None) -> dict:
    """只读报告 package、所有权与就绪状态。"""

    skills_root = _skills_root(skills_dir)
    target = _direct_target_path(skills_root)
    source_exists = _lexists(SOURCE_DIR / "SKILL.md")
    source_snapshot, source_error = (
        _snapshot_status(SOURCE_DIR) if source_exists else (None, None)
    )
    first = _status_observation(skills_root, target)
    observation = _status_observation(skills_root, target)
    readiness_changed = (
        first["readiness_signature"] != observation["readiness_signature"]
    )
    uninstall_state_changed = first["signature"] != observation["signature"]
    installed_snapshot: PackageSnapshot | None = observation["installed_snapshot"]
    ownership: OwnershipState = observation["ownership"]
    reason = _readiness_reason(
        observation,
        concurrent_change=readiness_changed,
    )
    uninstall_block_reason = _uninstall_block_reason(
        observation,
        concurrent_change=uninstall_state_changed,
    )

    result = {
        "ok": True,
        "action": "status",
        "source_exists": source_exists,
        "target_present": observation["target_present"],
        "installed": observation["installed"],
        "managed_by_installer": ownership.managed_by_installer,
        "ownership_valid": ownership.valid,
        "ready": reason is None,
        "reason": reason,
        "managed": observation["managed"],
        "uninstall_allowed": uninstall_block_reason is None,
        "uninstall_block_reason": uninstall_block_reason,
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
    if observation["installed_error"] is not None:
        result["installed_error"] = observation["installed_error"]
    if ownership.error_type is not None:
        result["ownership_error"] = {
            "error_type": ownership.error_type,
            "error": ownership.error,
        }
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
                    "unsafe_target",
                    "installed target contains a symlink or reparse point",
                )


def _delete_owned_state_record(
    skills_root: Path,
    target: Path,
    *,
    expected_fingerprint: str,
) -> None:
    """仅删除仍与已验证观察一致的安装器所有权记录。"""

    if _lexists(target):
        raise LocalSkillError(
            "ownership_mismatch",
            "target appeared before ownership record deletion",
        )
    current = _read_ownership_state(skills_root, target)
    if (
        not current.valid
        or current.fingerprint != expected_fingerprint
    ):
        raise LocalSkillError(
            "ownership_mismatch",
            "ownership record changed before deletion",
        )
    _, state_path = _ownership_paths(skills_root)
    try:
        state_path.unlink()
    except (OSError, RuntimeError) as exc:
        raise LocalSkillError(
            "state_write_failed",
            "ownership record could not be removed",
            details={"ownership_removed": False},
        ) from exc
    if _lexists(state_path):
        raise LocalSkillError(
            "state_write_failed",
            "ownership record still exists after deletion",
            details={"ownership_removed": False},
        )
    _cleanup_empty_state_dir(skills_root)


def _partial_state_cleanup_error(
    cause: Exception,
    *,
    message: str,
    target: Path,
) -> LocalSkillError:
    """报告目标已删除但安装器状态尚未完整清理。"""

    ownership_removed = bool(
        isinstance(cause, LocalSkillError)
        and cause.details.get("ownership_removed", False)
    )
    return LocalSkillError(
        "state_cleanup_failed",
        message,
        details={
            "partial": True,
            "target_removed": not _lexists(target),
            "ownership_removed": ownership_removed,
        },
    )


def _validate_uninstall_target(skills_root: Path, target: Path) -> Path:
    """重新确认卸载目标为固定的普通直接子目录。"""

    if _is_link_like(target) or not target.is_dir():
        raise LocalSkillError(
            "unsafe_target",
            "uninstall target must be a plain directory",
        )
    try:
        target_resolved = target.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LocalSkillError(
            "unsafe_target",
            "uninstall target could not be resolved",
        ) from exc
    if (
        target_resolved == skills_root
        or target_resolved.parent != skills_root
        or target_resolved.name != SKILL_NAME
        or target_resolved == SOURCE_DIR.resolve(strict=True)
        or target_resolved.is_relative_to(BUNDLED_SKILLS_ROOT)
    ):
        raise LocalSkillError(
            "unsafe_target",
            "uninstall target is not the expected direct child of skills root",
        )
    return target_resolved


def uninstall(skills_dir: str | None) -> dict:
    """仅删除所有权、revision 和治理状态均可确认的安装副本。"""

    skills_root = _skills_root(skills_dir)
    try:
        with acquire_skill_lock(skills_root, SKILL_NAME):
            locked_root = _skills_root(skills_dir)
            if locked_root != skills_root:
                raise LocalSkillError(
                    "unsafe_target",
                    "skills root changed while acquiring the Skill lock",
                )
            target = _target_path(locked_root)
            ownership = _read_ownership_state(locked_root, target)
            if not _lexists(target):
                if not ownership.exists:
                    return {
                        "ok": True,
                        "action": "uninstall",
                        "already_absent": True,
                        "reason": "target_missing",
                        "ownership_removed": False,
                        "installed_path": str(target),
                    }
                if not ownership.valid:
                    raise LocalSkillError(
                        ownership.error_type or "ownership_record_invalid",
                        ownership.error or "ownership record is invalid",
                    )
                try:
                    _delete_owned_state_record(
                        locked_root,
                        target,
                        expected_fingerprint=ownership.fingerprint,
                    )
                except LocalSkillError as exc:
                    raise _partial_state_cleanup_error(
                        exc,
                        message=(
                            "target was already absent but ownership state cleanup failed"
                        ),
                        target=target,
                    ) from exc
                return {
                    "ok": True,
                    "action": "uninstall",
                    "already_absent": True,
                    "reason": "target_missing",
                    "ownership_removed": True,
                    "installed_path": str(target),
                }

            target_resolved = _validate_uninstall_target(locked_root, target)
            if not ownership.exists:
                raise LocalSkillError(
                    "ownership_record_missing",
                    "uninstall refuses a target without an ownership record",
                )
            if not ownership.valid:
                raise LocalSkillError(
                    ownership.error_type or "ownership_record_invalid",
                    ownership.error or "ownership record is invalid",
                )
            if _governance_reason(target_resolved) is not None:
                raise LocalSkillError(
                    "skill_managed",
                    "uninstall refuses a governed, pinned, or adopted Skill",
                )
            try:
                installed_snapshot = _snapshot_installed_package(target_resolved)
            except (LocalSkillError, OSError, RuntimeError) as exc:
                raise LocalSkillError(
                    "target_changed",
                    "installed target layout or content cannot be verified",
                ) from exc
            if (
                installed_snapshot.package_revision
                != ownership.record["installed_revision"]
            ):
                raise LocalSkillError(
                    "target_changed",
                    "installed target differs from its ownership revision",
                )
            _assert_no_links_recursive(target_resolved)

            target_removed = False
            try:
                # 固定顺序：外层已持有 Skill 锁，随后才取得 trust store 锁。
                with acquire_trust_store_lock():
                    trust_state = inspect_skill_trust_state(SKILL_NAME)
                    trust_reason = _trust_block_reason(trust_state)
                    if trust_reason is not None:
                        raise LocalSkillError(
                            trust_reason,
                            "uninstall refuses a trusted or uncertain Skill state",
                        )
                    if _governance_reason(target_resolved) is not None:
                        raise LocalSkillError(
                            "skill_managed",
                            "Skill governance state changed during uninstall validation",
                        )
                    confirmed_target = _validate_uninstall_target(
                        locked_root,
                        target,
                    )
                    if confirmed_target != target_resolved:
                        raise LocalSkillError(
                            "unsafe_target",
                            "uninstall target identity changed during validation",
                        )
                    confirmed_ownership = _read_ownership_state(locked_root, target)
                    if (
                        not confirmed_ownership.valid
                        or confirmed_ownership.fingerprint != ownership.fingerprint
                    ):
                        raise LocalSkillError(
                            "ownership_mismatch",
                            "ownership record changed during uninstall validation",
                        )
                    try:
                        confirmed_snapshot = _snapshot_installed_package(
                            target_resolved
                        )
                    except (LocalSkillError, OSError, RuntimeError) as exc:
                        raise LocalSkillError(
                            "target_changed",
                            "installed target changed during uninstall validation",
                        ) from exc
                    if (
                        confirmed_snapshot.package_revision
                        != ownership.record["installed_revision"]
                    ):
                        raise LocalSkillError(
                            "target_changed",
                            "installed target changed during uninstall validation",
                        )
                    _assert_no_links_recursive(target_resolved)
                    try:
                        shutil.rmtree(target_resolved)
                    except OSError as exc:
                        raise LocalSkillError(
                            "delete_failed",
                            "installed target could not be removed completely",
                        ) from exc
                    if _lexists(target_resolved):
                        raise LocalSkillError(
                            "delete_failed",
                            "installed target still exists after deletion",
                        )
                    target_removed = True
            except LockTimeout as exc:
                raise LocalSkillError(
                    "trust_lock_timeout",
                    "could not acquire trust store lock",
                ) from exc
            except OSError as exc:
                if target_removed:
                    raise _partial_state_cleanup_error(
                        exc,
                        message=(
                            "target was removed but the trust lock could not be released"
                        ),
                        target=target,
                    ) from exc
                raise LocalSkillError(
                    "trust_store_unavailable",
                    "trust store lock could not be used reliably",
                ) from exc
            try:
                _delete_owned_state_record(
                    locked_root,
                    target,
                    expected_fingerprint=ownership.fingerprint,
                )
            except LocalSkillError as exc:
                raise _partial_state_cleanup_error(
                    exc,
                    message=(
                        "target was removed but ownership state cleanup failed"
                    ),
                    target=target,
                ) from exc
            return {
                "ok": True,
                "action": "uninstall",
                "already_absent": False,
                "reason": None,
                "ownership_removed": True,
                "installed_path": str(target_resolved),
            }
    except LockTimeout as exc:
        raise LocalSkillError(
            "lock_timeout",
            "could not acquire skill operation lock",
        ) from exc


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
        failure = dict(exc.details)
        failure.update(
            {
                "ok": False,
                "action": args.action,
                "error_type": exc.error_type,
                "error": exc.message,
            }
        )
        _print_result(failure)
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
