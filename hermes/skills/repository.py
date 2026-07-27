"""Skill 文件存储与路径安全边界。"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
from pathlib import Path

import yaml

from hermes._io_utils import LockTimeout, atomic_write_text, file_lock
from hermes.config import HERMES_HOME


SKILLS_DIR = HERMES_HOME / "skills"
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ALLOWED_FM_FIELDS = {"name", "description", "version", "platforms", "metadata"}


class SkillRepository:
    """只负责 Skill 文件的解析、读取和原子化修改。"""

    def __init__(self, skills_dir: Path | None = None):
        self.skills_dir = skills_dir if skills_dir is not None else SKILLS_DIR

    def _validate_name(self, name: str) -> tuple[bool, str]:
        if not name:
            return False, "name is empty"
        if name in (".", ".."):
            return False, f"name must not be {name!r}"
        if not _NAME_RE.match(name):
            return False, "name must match [A-Za-z0-9_-]+"
        return True, ""

    def _resolve_skill_dir(self, name: str) -> tuple[Path | None, str]:
        ok, reason = self._validate_name(name)
        if not ok:
            return None, reason
        root_real = self.skills_dir.resolve()
        target_real = (self.skills_dir / name).resolve()
        if target_real == root_real:
            return None, "name resolves to skills root"
        if not str(target_real).startswith(str(root_real) + os.sep):
            return None, "resolved path escapes skills root"
        if target_real.parent != root_real:
            return None, "name must be a direct child of skills root"
        return target_real, ""

    def _skill_lock_target(self, name: str) -> Path:
        """返回操作锁目标，file_lock 会生成 ``.locks/<name>.lock``。"""
        return self.skills_dir / ".locks" / name

    @contextlib.contextmanager
    def _skill_operation_lock(self, name: str):
        """为同一个 Skill 的全部写操作提供唯一的跨进程锁。"""
        self._skill_lock_target(name).parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self._skill_lock_target(name)):
            yield

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
        if not self.skills_dir.exists():
            return []
        out: list[dict] = []
        for skill_dir in sorted(self.skills_dir.iterdir()):
            if not skill_dir.is_dir() or not self._validate_name(skill_dir.name)[0]:
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            text = skill_file.read_text(encoding="utf-8")
            metadata, _, error = self.parse_frontmatter_safe(text)
            entry = {"name": metadata.get("name", skill_dir.name) if not error else skill_dir.name,
                     "description": metadata.get("description", "") if not error else "",
                     "relative_path": f"skills/{skill_dir.name}"}
            if error:
                entry["error"] = error
            else:
                for key in ("version", "platforms", "metadata"):
                    if key in metadata:
                        entry[key] = metadata[key]
            out.append(entry)
        return out

    def load(self, name: str) -> dict:
        skill_dir, reason = self._resolve_skill_dir(name)
        if skill_dir is None:
            return {"ok": False, "error_type": "invalid_name", "error": reason}
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            return {"ok": False, "error_type": "not_found", "error": f"skill {name!r} does not exist", "name": name}
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
                "body": body, "content": text}

    def create(self, name: str, **kwargs) -> dict:
        skill_dir, reason = self._resolve_skill_dir(name)
        if skill_dir is None:
            return {"ok": False, "error_type": "invalid_name", "error": reason}
        content = self._render_skill(name, kwargs.get("body", ""), description=kwargs.get("description", ""),
                                     version=kwargs.get("version"), platforms=kwargs.get("platforms"), metadata=kwargs.get("metadata"))
        try:
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            with self._skill_operation_lock(name):
                skill_dir, reason = self._resolve_skill_dir(name)
                if skill_dir is None:
                    return {"ok": False, "error_type": "invalid_name", "error": reason}
                skill_file = skill_dir / "SKILL.md"
                if skill_dir.exists() or skill_file.exists():
                    return {"ok": False, "error_type": "exists", "error": f"skill {name!r} was created concurrently", "name": name}
                skill_dir.mkdir(parents=True, exist_ok=True)
                atomic_write_text(skill_file, content)
        except LockTimeout:
            return {"ok": False, "error_type": "lock_timeout", "error": "could not acquire skill file lock"}
        except OSError as exc:
            return {"ok": False, "error_type": "io_error", "error": str(exc)}
        return {"ok": True, "name": name, "action": "create", "size": len(content)}

    def edit(self, name: str, **kwargs) -> dict:
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
                text = skill_file.read_text(encoding="utf-8")
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
            return {"ok": False, "error_type": "lock_timeout", "error": "could not acquire skill file lock"}
        except OSError as exc:
            return {"ok": False, "error_type": "io_error", "error": str(exc)}
        return {"ok": True, "name": name, "action": "edit", "size": len(content)}

    def patch(self, name: str, **kwargs) -> dict:
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
                text = skill_file.read_text(encoding="utf-8")
                count = text.count(old_text)
                if count == 0:
                    return {"ok": False, "error_type": "no_match", "error": f"old_text not found in skill {name!r}", "name": name}
                if count > 1:
                    return {"ok": False, "error_type": "ambiguous_match", "error": f"old_text appears {count} times; provide more specific old_text", "name": name, "match_count": count}
                content = text.replace(old_text, new_text, 1)
                atomic_write_text(skill_file, content)
        except LockTimeout:
            return {"ok": False, "error_type": "lock_timeout", "error": "could not acquire skill file lock"}
        except OSError as exc:
            return {"ok": False, "error_type": "io_error", "error": str(exc)}
        return {"ok": True, "name": name, "action": "patch", "size": len(content)}

    def delete(self, name: str) -> dict:
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
                real_target, real_root = skill_dir.resolve(), self.skills_dir.resolve()
                if real_target == real_root:
                    return {"ok": False, "error_type": "forbidden", "error": "cannot delete skills root directory"}
                if real_target.parent != real_root:
                    return {"ok": False, "error_type": "forbidden", "error": "target is not a direct child of skills root"}
                shutil.rmtree(skill_dir)
        except OSError as exc:
            return {"ok": False, "error_type": "io_error", "error": str(exc)}
        return {"ok": True, "name": name, "action": "delete"}
