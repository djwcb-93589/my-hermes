"""Skill 文件存储与路径安全边界。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import yaml

from hermes._io_utils import LockTimeout, atomic_write_text
from hermes.config import HERMES_HOME
from hermes.skill_locking import acquire_skill_lock, skill_lock_target


SKILLS_DIR = HERMES_HOME / "skills"
BUNDLED_SKILLS_DIR = (
    Path(__file__).resolve().parent / "bundled" / "skills"
)
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ALLOWED_FM_FIELDS = {"name", "description", "version", "platforms", "metadata"}
_GOVERNANCE_FILE = ".myhermes.json"
_LEGACY_GOVERNANCE_REVISION = "legacy"
_SUPPORT_DIRS = frozenset({"references", "templates", "scripts", "assets"})
_PROTECTED_SKILL_FILES = frozenset({"SKILL.md", _GOVERNANCE_FILE})


class SkillRepository:
    """读取独立的用户与 bundled Skill，并只修改用户 Skill。"""

    def __init__(
        self,
        skills_dir: Path | None = None,
        bundled_skills_dir: Path | None = None,
    ):
        self.skills_dir = skills_dir if skills_dir is not None else SKILLS_DIR
        self.bundled_skills_dir = (
            bundled_skills_dir
            if bundled_skills_dir is not None
            else BUNDLED_SKILLS_DIR
        )

    def _validate_name(self, name: str) -> tuple[bool, str]:
        if not name:
            return False, "name is empty"
        if name in (".", ".."):
            return False, f"name must not be {name!r}"
        if not _NAME_RE.match(name):
            return False, "name must match [A-Za-z0-9_-]+"
        return True, ""

    def _resolve_skill_dir(self, name: str) -> tuple[Path | None, str]:
        """解析可写的用户 Skill；所有写操作继续使用这一根目录。"""

        return self._resolve_skill_dir_in(self.skills_dir, name)

    def _resolve_bundled_skill_dir(
        self,
        name: str,
    ) -> tuple[Path | None, str]:
        """解析随发布物交付的只读 bundled Skill。"""

        return self._resolve_skill_dir_in(self.bundled_skills_dir, name)

    def _resolve_skill_dir_in(
        self,
        root: Path,
        name: str,
    ) -> tuple[Path | None, str]:
        ok, reason = self._validate_name(name)
        if not ok:
            return None, reason
        root_real = root.resolve()
        target_real = (root / name).resolve()
        if target_real == root_real:
            return None, "name resolves to skills root"
        if not str(target_real).startswith(str(root_real) + os.sep):
            return None, "resolved path escapes skills root"
        if target_real.parent != root_real:
            return None, "name must be a direct child of skills root"
        return target_real, ""

    def _resolve_read_skill_dir(
        self,
        name: str,
    ) -> tuple[Path | None, str | None, str]:
        """按用户优先、bundled 回退规则解析当前可见 Skill。"""

        local_dir, reason = self._resolve_skill_dir(name)
        if local_dir is None:
            return None, None, reason
        if (local_dir / "SKILL.md").is_file():
            return local_dir, "local", ""
        bundled_dir, reason = self._resolve_bundled_skill_dir(name)
        if bundled_dir is None:
            return None, None, reason
        if (bundled_dir / "SKILL.md").is_file():
            return bundled_dir, "bundled", ""
        return None, None, ""

    def _resolve_support_file(
        self,
        name: str,
        relative_path: str,
    ) -> tuple[Path | None, str]:
        """解析可写用户 Skill 的 supporting file。"""

        skill_dir, reason = self._resolve_skill_dir(name)
        if skill_dir is None:
            return None, reason
        return self._resolve_support_file_in(
            skill_dir,
            relative_path,
        )

    def _resolve_support_file_in(
        self,
        skill_dir: Path,
        relative_path: str,
    ) -> tuple[Path | None, str]:
        """解析允许的 package 文件，并拒绝路径与符号链接逃逸。"""

        if not isinstance(relative_path, str) or not relative_path:
            return None, "relative_path is required"
        normalized = relative_path.replace("\\", "/")
        if normalized.startswith("/") or Path(normalized).is_absolute():
            return None, "relative_path must be relative"
        parts = normalized.split("/")
        if len(parts) < 2 or any(part in {"", ".", ".."} for part in parts):
            return None, "relative_path must name a file below an allowed directory"
        if parts[0] not in _SUPPORT_DIRS:
            return None, "relative_path must start with an allowed support directory"
        if any(part in _PROTECTED_SKILL_FILES or part == ".locks" for part in parts):
            return None, "relative_path targets a protected skill file"
        top_directory = skill_dir / parts[0]
        current = skill_dir
        for part in parts:
            current = current / part
            # 已存在的任何组件（包括损坏链接）都不允许是符号链接。
            if current.is_symlink():
                return None, "relative_path must not traverse symbolic links"
        target = skill_dir / Path(*parts)
        try:
            target_real = target.resolve()
            top_real = top_directory.resolve()
            target_real.relative_to(top_real)
        except ValueError:
            return None, "resolved path escapes the requested support directory"
        except (OSError, RuntimeError):
            return None, "relative_path could not be resolved safely"
        return target_real, ""

    def _skill_lock_target(self, name: str) -> Path:
        """返回操作锁目标，file_lock 会生成 ``.locks/<name>.lock``。"""
        return skill_lock_target(self.skills_dir, name)

    def _skill_operation_lock(self, name: str):
        """为同一个 Skill 的全部写操作提供唯一的跨进程锁。"""
        return acquire_skill_lock(self.skills_dir, name)

    @staticmethod
    def _governance_payload(record: dict) -> str:
        return json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    @staticmethod
    def _split_frontmatter(text: str) -> tuple[str, str] | None:
        if not text.startswith("---"):
            return None
        lines = text.splitlines(keepends=True)
        if not lines or lines[0].strip() != "---":
            return None
        fm_lines: list[str] = []
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                return "".join(fm_lines), "".join(lines[index + 1:]).lstrip("\n")
            fm_lines.append(line)
        return None

    @classmethod
    def parse_frontmatter_safe(cls, text: str) -> tuple[dict, str, str | None]:
        split = cls._split_frontmatter(text)
        if split is None:
            return {}, text.lstrip(), None
        fm_text, body = split
        try:
            data = yaml.safe_load(fm_text) or {}
        except yaml.YAMLError as exc:
            return {}, text, f"frontmatter yaml parse error: {exc}"
        if not isinstance(data, dict):
            return {}, text, "frontmatter top level must be a mapping"
        return {key: value for key, value in data.items() if key in _ALLOWED_FM_FIELDS}, body, None

    @staticmethod
    def _render_skill(name: str, body: str, *, description: str = "", version=None,
                      platforms=None, metadata=None) -> str:
        fm: dict = {"name": name}
        if description:
            fm["description"] = description
        if version:
            fm["version"] = version
        if platforms:
            fm["platforms"] = platforms
        if metadata:
            fm["metadata"] = metadata
        fm_text = yaml.safe_dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False).strip()
        return f"---\n{fm_text}\n---\n\n{body}"

    def discover(self) -> list[dict]:
        """合并 bundled 与用户目录；同名用户 Skill 稳定覆盖 bundled。"""

        discovered: dict[str, dict] = {}
        for root in (self.bundled_skills_dir, self.skills_dir):
            for directory_name, entry in self._discover_root(root):
                discovered[directory_name] = entry
        return [discovered[name] for name in sorted(discovered)]

    def _discover_root(self, root: Path) -> list[tuple[str, dict]]:
        if not root.exists() or not root.is_dir():
            return []
        out: list[tuple[str, dict]] = []
        for skill_dir in sorted(root.iterdir()):
            if not skill_dir.is_dir() or not self._validate_name(skill_dir.name)[0]:
                continue
            resolved_dir, _ = self._resolve_skill_dir_in(
                root,
                skill_dir.name,
            )
            if resolved_dir is None or resolved_dir != skill_dir.resolve():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                continue
            text = skill_file.read_text(encoding="utf-8")
            metadata, _, error = self.parse_frontmatter_safe(text)
            entry = {"name": metadata.get("name", skill_dir.name) if not error else skill_dir.name,
                     "description": metadata.get("description", "") if not error else "",
                     "relative_path": f"skills/{skill_dir.name}",
                     "revision": hashlib.sha256(text.encode("utf-8")).hexdigest()}
            if error:
                entry["error"] = error
            else:
                for key in ("version", "platforms", "metadata"):
                    if key in metadata:
                        entry[key] = metadata[key]
            out.append((skill_dir.name, entry))
        return out

    def load(self, name: str) -> dict:
        skill_dir, _, reason = self._resolve_read_skill_dir(name)
        if skill_dir is None:
            if reason:
                return {
                    "ok": False,
                    "error_type": "invalid_name",
                    "error": reason,
                }
            return {
                "ok": False,
                "error_type": "not_found",
                "error": f"skill {name!r} does not exist",
                "name": name,
            }
        skill_file = skill_dir / "SKILL.md"
        try:
            text = skill_file.read_text(encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error_type": "io_error", "error": str(exc), "name": name}
        metadata, body, error = self.parse_frontmatter_safe(text)
        if error:
            return {"ok": False, "error_type": "parse_error", "error": error, "name": name,
                    "relative_path": f"skills/{name}"}
        return {"ok": True, "name": metadata.get("name", name), "relative_path": f"skills/{name}",
                "description": metadata.get("description", ""), "version": metadata.get("version"),
                "platforms": metadata.get("platforms"), "metadata": metadata.get("metadata"),
                "body": body, "content": text,
                "revision": hashlib.sha256(text.encode("utf-8")).hexdigest()}

    def resolve_source(self, name: str) -> dict:
        """返回覆盖规则选中的实际 Skill 来源。"""

        skill_dir, source, reason = self._resolve_read_skill_dir(name)
        if skill_dir is None:
            if reason:
                return {
                    "ok": False,
                    "error_type": "invalid_name",
                    "error": reason,
                }
            return {"ok": False, "error_type": "not_found", "error": f"skill {name!r} does not exist", "name": name}
        return {"ok": True, "source": source}

    def _read_governance_record(self, skill_dir: Path, name: str) -> dict:
        sidecar = skill_dir / _GOVERNANCE_FILE
        if not sidecar.exists():
            return {"ok": True, "record": None, "legacy": True,
                    "governance_revision": _LEGACY_GOVERNANCE_REVISION}
        try:
            raw_record = sidecar.read_text(encoding="utf-8")
            record = json.loads(raw_record)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return {"ok": False, "error_type": "governance_invalid", "error": f"invalid governance record: {exc}", "name": name}
        if not isinstance(record, dict):
            return {"ok": False, "error_type": "governance_invalid", "error": "governance record must be an object", "name": name}
        return {"ok": True, "record": record, "legacy": False,
                "governance_revision": hashlib.sha256(raw_record.encode("utf-8")).hexdigest()}

    @staticmethod
    def _revision_conflict(name: str) -> dict:
        return {"ok": False, "error_type": "revision_conflict", "error": "skill content changed concurrently", "name": name}

    @staticmethod
    def _governance_conflict(name: str) -> dict:
        return {"ok": False, "error_type": "governance_conflict", "error": "governance record changed concurrently", "name": name}

    def _validate_governance_locked(self, skill_dir: Path, name: str, expected_governance_revision: str) -> dict:
        current = self._read_governance_record(skill_dir, name)
        if not current.get("ok"):
            return current
        if current["governance_revision"] != expected_governance_revision:
            return self._governance_conflict(name)
        return {"ok": True, "governance_revision": current["governance_revision"]}

    def load_governance_record(self, name: str) -> dict:
        """读取治理 sidecar；缺失时显式标记为 legacy，绝不自动写入。"""
        skill_dir, _, reason = self._resolve_read_skill_dir(name)
        if skill_dir is None:
            if reason:
                return {
                    "ok": False,
                    "error_type": "invalid_name",
                    "error": reason,
                }
            return {"ok": False, "error_type": "not_found", "error": f"skill {name!r} does not exist", "name": name}
        return self._read_governance_record(skill_dir, name)

    def validate_governance_revision(self, name: str, *, expected_governance_revision: str) -> dict:
        """在统一操作锁内确认 no-op 观察到的治理版本尚未变化。"""
        skill_dir, reason = self._resolve_skill_dir(name)
        if skill_dir is None:
            return {"ok": False, "error_type": "invalid_name", "error": reason}
        try:
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            with self._skill_operation_lock(name):
                skill_dir, reason = self._resolve_skill_dir(name)
                if skill_dir is None:
                    return {"ok": False, "error_type": "invalid_name", "error": reason}
                if not (skill_dir / "SKILL.md").exists():
                    return {"ok": False, "error_type": "not_found", "error": f"skill {name!r} does not exist", "name": name}
                return self._validate_governance_locked(skill_dir, name, expected_governance_revision)
        except LockTimeout:
            return {"ok": False, "error_type": "lock_timeout", "error": "could not acquire skill operation lock"}
        except OSError as exc:
            return {"ok": False, "error_type": "io_error", "error": str(exc)}

    def write_governance_record(self, name: str, record: dict, *, expected_governance_revision: str) -> dict:
        """在统一 Skill 操作锁内原子写入治理 sidecar。"""
        skill_dir, reason = self._resolve_skill_dir(name)
        if skill_dir is None:
            return {"ok": False, "error_type": "invalid_name", "error": reason}
        try:
            payload = self._governance_payload(record)
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            with self._skill_operation_lock(name):
                skill_dir, reason = self._resolve_skill_dir(name)
                if skill_dir is None:
                    return {"ok": False, "error_type": "invalid_name", "error": reason}
                skill_file = skill_dir / "SKILL.md"
                if not skill_file.exists():
                    return {"ok": False, "error_type": "not_found", "error": f"skill {name!r} does not exist", "name": name}
                current = self._validate_governance_locked(skill_dir, name, expected_governance_revision)
                if not current.get("ok"):
                    return current
                atomic_write_text(skill_dir / _GOVERNANCE_FILE, payload)
        except LockTimeout:
            return {"ok": False, "error_type": "lock_timeout", "error": "could not acquire skill operation lock"}
        except (OSError, TypeError, ValueError) as exc:
            return {"ok": False, "error_type": "io_error", "error": str(exc)}
        return {"ok": True, "name": name}

    def list_support_files(self, name: str) -> dict:
        """返回允许 package 目录中的安全相对文件路径。"""
        skill_dir, _, reason = self._resolve_read_skill_dir(name)
        if skill_dir is None:
            if reason:
                return {
                    "ok": False,
                    "error_type": "invalid_name",
                    "error": reason,
                }
            return {"ok": False, "error_type": "not_found", "error": f"skill {name!r} does not exist", "name": name}
        files: list[str] = []
        try:
            for directory in _SUPPORT_DIRS:
                root = skill_dir / directory
                if not root.is_dir() or root.is_symlink():
                    continue
                for path in root.rglob("*"):
                    if not path.is_file():
                        continue
                    resolved, _ = self._resolve_support_file_in(
                        skill_dir,
                        path.relative_to(skill_dir).as_posix(),
                    )
                    if resolved is not None and resolved == path.resolve():
                        files.append(path.relative_to(skill_dir).as_posix())
        except OSError as exc:
            return {"ok": False, "error_type": "io_error", "error": str(exc), "name": name}
        return {"ok": True, "name": name, "support_files": sorted(files)}

    def read_support_file(self, name: str, relative_path: str) -> dict:
        """读取一个 UTF-8 supporting file，并从本次读取生成 revision。"""
        skill_dir, _, reason = self._resolve_read_skill_dir(name)
        if skill_dir is None:
            if reason:
                return {
                    "ok": False,
                    "error_type": "invalid_path",
                    "error": reason,
                    "name": name,
                }
            return {
                "ok": False,
                "error_type": "not_found",
                "error": f"skill {name!r} does not exist",
                "name": name,
            }
        target, reason = self._resolve_support_file_in(
            skill_dir,
            relative_path,
        )
        if target is None:
            return {"ok": False, "error_type": "invalid_path", "error": reason, "name": name}
        if not target.exists() or not target.is_file():
            return {"ok": False, "error_type": "not_found", "error": f"support file {relative_path!r} does not exist", "name": name}
        try:
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return {"ok": False, "error_type": "io_error", "error": str(exc), "name": name}
        return {
            "ok": True,
            "name": name,
            "relative_path": relative_path.replace("\\", "/"),
            "content": content,
            "revision": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }

    def write_support_file(
        self,
        name: str,
        relative_path: str,
        content: str,
        *,
        expected_governance_revision: str,
        expected_revision: str | None = None,
    ) -> dict:
        """在统一锁内写入 UTF-8 supporting file，并执行双版本校验。"""
        target, reason = self._resolve_support_file(name, relative_path)
        if target is None:
            return {"ok": False, "error_type": "invalid_path", "error": reason, "name": name}
        if not isinstance(content, str):
            return {"ok": False, "error_type": "invalid_args", "error": "content must be a string", "name": name}
        try:
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            with self._skill_operation_lock(name):
                target, reason = self._resolve_support_file(name, relative_path)
                if target is None:
                    return {"ok": False, "error_type": "invalid_path", "error": reason, "name": name}
                skill_dir, _ = self._resolve_skill_dir(name)
                if skill_dir is None or not (skill_dir / "SKILL.md").exists():
                    return {"ok": False, "error_type": "not_found", "error": f"skill {name!r} does not exist", "name": name}
                governance = self._validate_governance_locked(skill_dir, name, expected_governance_revision)
                if not governance.get("ok"):
                    return governance
                if target.exists():
                    if not target.is_file():
                        return {"ok": False, "error_type": "invalid_path", "error": "relative_path is not a file", "name": name}
                    current = target.read_text(encoding="utf-8")
                    if (
                        expected_revision is None
                        or hashlib.sha256(current.encode("utf-8")).hexdigest()
                        != expected_revision
                    ):
                        return self._revision_conflict(name)
                elif expected_revision is not None:
                    return self._revision_conflict(name)
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(target, content)
        except LockTimeout:
            return {"ok": False, "error_type": "lock_timeout", "error": "could not acquire skill operation lock"}
        except (OSError, UnicodeError) as exc:
            return {"ok": False, "error_type": "io_error", "error": str(exc), "name": name}
        return {"ok": True, "name": name, "action": "write_file",
                "relative_path": relative_path.replace("\\", "/"), "size": len(content),
                "revision": hashlib.sha256(content.encode("utf-8")).hexdigest()}

    def remove_support_file(
        self,
        name: str,
        relative_path: str,
        *,
        expected_governance_revision: str,
        expected_revision: str | None = None,
    ) -> dict:
        """在统一锁内删除 supporting file，且绝不删除 Skill 根或顶层目录。"""
        target, reason = self._resolve_support_file(name, relative_path)
        if target is None:
            return {"ok": False, "error_type": "invalid_path", "error": reason, "name": name}
        try:
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            with self._skill_operation_lock(name):
                target, reason = self._resolve_support_file(name, relative_path)
                if target is None:
                    return {"ok": False, "error_type": "invalid_path", "error": reason, "name": name}
                skill_dir, _ = self._resolve_skill_dir(name)
                if skill_dir is None or not (skill_dir / "SKILL.md").exists():
                    return {"ok": False, "error_type": "not_found", "error": f"skill {name!r} does not exist", "name": name}
                governance = self._validate_governance_locked(skill_dir, name, expected_governance_revision)
                if not governance.get("ok"):
                    return governance
                if not target.exists() or not target.is_file():
                    return {"ok": False, "error_type": "not_found", "error": f"support file {relative_path!r} does not exist", "name": name}
                current = target.read_text(encoding="utf-8")
                if expected_revision is not None and hashlib.sha256(current.encode("utf-8")).hexdigest() != expected_revision:
                    return self._revision_conflict(name)
                target.unlink()
                top_directory = skill_dir / target.relative_to(skill_dir).parts[0]
                parent = target.parent
                while parent != top_directory:
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
        except LockTimeout:
            return {"ok": False, "error_type": "lock_timeout", "error": "could not acquire skill operation lock"}
        except (OSError, UnicodeError) as exc:
            return {"ok": False, "error_type": "io_error", "error": str(exc), "name": name}
        return {"ok": True, "name": name, "action": "remove_file", "relative_path": relative_path.replace("\\", "/")}

    def create(self, name: str, **kwargs) -> dict:
        skill_dir, reason = self._resolve_skill_dir(name)
        if skill_dir is None:
            return {"ok": False, "error_type": "invalid_name", "error": reason}
        governance_record = kwargs.pop("governance_record", None)
        if not isinstance(governance_record, dict):
            return {"ok": False, "error_type": "invalid_args", "error": "governance_record is required for create"}
        content = self._render_skill(name, kwargs.get("body", ""), description=kwargs.get("description", ""),
                                     version=kwargs.get("version"), platforms=kwargs.get("platforms"), metadata=kwargs.get("metadata"))
        temp_dir: Path | None = None
        try:
            governance_payload = self._governance_payload(governance_record)
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            with self._skill_operation_lock(name):
                skill_dir, reason = self._resolve_skill_dir(name)
                if skill_dir is None:
                    return {"ok": False, "error_type": "invalid_name", "error": reason}
                skill_file = skill_dir / "SKILL.md"
                if skill_dir.exists() or skill_file.exists():
                    return {"ok": False, "error_type": "exists", "error": f"skill {name!r} was created concurrently", "name": name}
                temp_dir = Path(
                    tempfile.mkdtemp(
                        prefix=".hs-",
                        dir=self.skills_dir,
                    )
                )
                atomic_write_text(temp_dir / "SKILL.md", content)
                atomic_write_text(temp_dir / _GOVERNANCE_FILE, governance_payload)
                os.replace(temp_dir, skill_dir)
                temp_dir = None
        except LockTimeout:
            return {"ok": False, "error_type": "lock_timeout", "error": "could not acquire skill operation lock"}
        except (OSError, TypeError, ValueError) as exc:
            return {"ok": False, "error_type": "io_error", "error": str(exc)}
        finally:
            if temp_dir is not None and temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
        return {"ok": True, "name": name, "action": "create", "size": len(content)}

    def edit(self, name: str, *, expected_governance_revision: str, expected_revision: str | None = None, **kwargs) -> dict:
        skill_dir, reason = self._resolve_skill_dir(name)
        if skill_dir is None:
            return {"ok": False, "error_type": "invalid_name", "error": reason}
        try:
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            with self._skill_operation_lock(name):
                skill_dir, reason = self._resolve_skill_dir(name)
                if skill_dir is None:
                    return {"ok": False, "error_type": "invalid_name", "error": reason}
                skill_file = skill_dir / "SKILL.md"
                if not skill_file.exists():
                    return {"ok": False, "error_type": "not_found", "error": f"skill {name!r} does not exist", "name": name}
                current = self._validate_governance_locked(skill_dir, name, expected_governance_revision)
                if not current.get("ok"):
                    return current
                text = skill_file.read_text(encoding="utf-8")
                if expected_revision is not None and hashlib.sha256(text.encode("utf-8")).hexdigest() != expected_revision:
                    return self._revision_conflict(name)
                metadata, old_body, error = self.parse_frontmatter_safe(text)
                if error:
                    return {"ok": False, "error_type": "parse_error", "error": error, "name": name}
                content = self._render_skill(name, kwargs.get("body") if kwargs.get("body") is not None else old_body,
                    description=kwargs["description"] if kwargs.get("description") is not None else metadata.get("description", ""),
                    version=kwargs.get("version") if kwargs.get("version") is not None else metadata.get("version"),
                    platforms=kwargs.get("platforms") if kwargs.get("platforms") is not None else metadata.get("platforms"),
                    metadata=kwargs.get("metadata") if kwargs.get("metadata") is not None else metadata.get("metadata"))
                atomic_write_text(skill_file, content)
        except LockTimeout:
            return {"ok": False, "error_type": "lock_timeout", "error": "could not acquire skill operation lock"}
        except OSError as exc:
            return {"ok": False, "error_type": "io_error", "error": str(exc)}
        return {"ok": True, "name": name, "action": "edit", "size": len(content)}

    def patch(self, name: str, *, expected_governance_revision: str, expected_revision: str | None = None, **kwargs) -> dict:
        skill_dir, reason = self._resolve_skill_dir(name)
        if skill_dir is None:
            return {"ok": False, "error_type": "invalid_name", "error": reason}
        old_text, new_text = kwargs.get("old_text", ""), kwargs.get("new_text", "")
        if not old_text:
            return {"ok": False, "error_type": "invalid_args", "error": "old_text is required for patch"}
        if new_text == old_text:
            return {"ok": False, "error_type": "invalid_args", "error": "new_text must differ from old_text"}
        try:
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            with self._skill_operation_lock(name):
                skill_dir, reason = self._resolve_skill_dir(name)
                if skill_dir is None:
                    return {"ok": False, "error_type": "invalid_name", "error": reason}
                skill_file = skill_dir / "SKILL.md"
                if not skill_file.exists():
                    return {"ok": False, "error_type": "not_found", "error": f"skill {name!r} does not exist", "name": name}
                current = self._validate_governance_locked(skill_dir, name, expected_governance_revision)
                if not current.get("ok"):
                    return current
                text = skill_file.read_text(encoding="utf-8")
                if expected_revision is not None and hashlib.sha256(text.encode("utf-8")).hexdigest() != expected_revision:
                    return self._revision_conflict(name)
                count = text.count(old_text)
                if count == 0:
                    return {"ok": False, "error_type": "no_match", "error": f"old_text not found in skill {name!r}", "name": name}
                if count > 1:
                    return {"ok": False, "error_type": "ambiguous_match", "error": f"old_text appears {count} times; provide more specific old_text", "name": name, "match_count": count}
                content = text.replace(old_text, new_text, 1)
                atomic_write_text(skill_file, content)
        except LockTimeout:
            return {"ok": False, "error_type": "lock_timeout", "error": "could not acquire skill operation lock"}
        except OSError as exc:
            return {"ok": False, "error_type": "io_error", "error": str(exc)}
        return {"ok": True, "name": name, "action": "patch", "size": len(content)}

    def delete(self, name: str, *, expected_governance_revision: str, expected_revision: str | None = None) -> dict:
        skill_dir, reason = self._resolve_skill_dir(name)
        if skill_dir is None:
            return {"ok": False, "error_type": "invalid_name", "error": reason}
        try:
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            with self._skill_operation_lock(name):
                skill_dir, reason = self._resolve_skill_dir(name)
                if skill_dir is None:
                    return {"ok": False, "error_type": "invalid_name", "error": reason}
                if not skill_dir.exists():
                    return {"ok": False, "error_type": "not_found", "error": f"skill {name!r} does not exist", "name": name}
                current = self._validate_governance_locked(skill_dir, name, expected_governance_revision)
                if not current.get("ok"):
                    return current
                skill_file = skill_dir / "SKILL.md"
                if not skill_file.exists():
                    return {"ok": False, "error_type": "not_found", "error": f"skill {name!r} does not exist", "name": name}
                text = skill_file.read_text(encoding="utf-8")
                if expected_revision is not None and hashlib.sha256(text.encode("utf-8")).hexdigest() != expected_revision:
                    return self._revision_conflict(name)
                real_target, real_root = skill_dir.resolve(), self.skills_dir.resolve()
                if real_target == real_root:
                    return {"ok": False, "error_type": "forbidden", "error": "cannot delete skills root directory"}
                if real_target.parent != real_root:
                    return {"ok": False, "error_type": "forbidden", "error": "target is not a direct child of skills root"}
                shutil.rmtree(skill_dir)
        except LockTimeout:
            return {"ok": False, "error_type": "lock_timeout", "error": "could not acquire skill operation lock"}
        except OSError as exc:
            return {"ok": False, "error_type": "io_error", "error": str(exc)}
        return {"ok": True, "name": name, "action": "delete"}
