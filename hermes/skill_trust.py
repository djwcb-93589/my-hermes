"""Skill 信任存储的共享锁与保守只读检查。"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from hermes._io_utils import DEFAULT_LOCK_POLL, DEFAULT_LOCK_TIMEOUT, file_lock
from hermes.config_values import hermes_home


TRUST_STORE_FILENAME = "trusted_skills.json"
TRUST_STORE_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_TRUST_STORE_PATH = hermes_home() / TRUST_STORE_FILENAME
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class SkillTrustStoreState:
    """指定 Skill 在信任存储中的保守只读状态。"""

    reliable: bool
    record_present: bool
    fingerprint: str | None = None
    error_type: str | None = None
    error: str | None = None


def trust_store_path(file_path: Path | None = None) -> Path:
    """返回写入端与只读检查共同使用的信任存储路径。"""

    return (
        Path(file_path)
        if file_path is not None
        else DEFAULT_TRUST_STORE_PATH
    )


@contextlib.contextmanager
def acquire_trust_store_lock(
    file_path: Path | None = None,
    *,
    timeout: float = DEFAULT_LOCK_TIMEOUT,
    poll: float = DEFAULT_LOCK_POLL,
) -> Iterator[None]:
    """使用信任写入端的同一文件锁保护完整临界区。"""

    target = trust_store_path(file_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(target, timeout=timeout, poll=poll):
        yield


def load_trust_records(file_path: Path | None = None) -> dict[str, dict]:
    """按既有容错语义读取写入端使用的信任记录。"""

    target = trust_store_path(file_path)
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {name: record for name, record in data.items() if isinstance(record, dict)}


def _is_link_like(path: Path) -> bool:
    """识别符号链接、junction 与其他重解析点。"""

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


def _unreliable(error_type: str, error: str) -> SkillTrustStoreState:
    """构造不泄露信任文件内容的失败状态。"""

    return SkillTrustStoreState(
        reliable=False,
        record_present=False,
        error_type=error_type,
        error=error,
    )


def _stat_signature(file_stat: os.stat_result) -> tuple[int, int, int, int, int]:
    """生成一次读取前后可比较的文件身份签名。"""

    return (
        file_stat.st_mode,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_dev,
        file_stat.st_ino,
    )


def _record_is_valid(record: object) -> bool:
    """验证当前信任写入端生成的最小记录结构。"""

    if not isinstance(record, dict):
        return False
    content_hash = record.get("content_hash")
    trusted_at = record.get("trusted_at")
    return bool(
        isinstance(content_hash, str)
        and _SHA256_RE.fullmatch(content_hash)
        and isinstance(trusted_at, str)
        and trusted_at
    )


def inspect_skill_trust_state(
    skill_name: str,
    *,
    file_path: Path | None = None,
) -> SkillTrustStoreState:
    """严格读取指定 Skill 的记录；任何不确定性均显式返回失败状态。"""

    if not isinstance(skill_name, str) or not _SKILL_NAME_RE.fullmatch(skill_name):
        raise ValueError("skill name must match [A-Za-z0-9_-]+")
    target = trust_store_path(file_path)
    try:
        before = target.lstat()
    except FileNotFoundError:
        return SkillTrustStoreState(reliable=True, record_present=False)
    except OSError:
        return _unreliable(
            "trust_store_unavailable",
            "trust store could not be inspected reliably",
        )
    if _is_link_like(target):
        return _unreliable(
            "trust_store_invalid",
            "trust store must not be a symlink or reparse point",
        )
    try:
        if not stat.S_ISREG(before.st_mode) or before.st_size > TRUST_STORE_MAX_BYTES:
            return _unreliable(
                "trust_store_invalid",
                "trust store is not a bounded regular file",
            )
        raw_bytes = target.read_bytes()
        after = target.lstat()
    except FileNotFoundError:
        return _unreliable(
            "trust_state_unknown",
            "trust store changed during inspection",
        )
    except OSError:
        return _unreliable(
            "trust_store_unavailable",
            "trust store could not be read reliably",
        )
    if (
        _is_link_like(target)
        or _stat_signature(before) != _stat_signature(after)
    ):
        return _unreliable(
            "trust_state_unknown",
            "trust store changed during inspection",
        )
    if len(raw_bytes) > TRUST_STORE_MAX_BYTES:
        return _unreliable(
            "trust_store_invalid",
            "trust store exceeds the size limit",
        )
    fingerprint = hashlib.sha256(raw_bytes).hexdigest()
    try:
        records = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return _unreliable(
            "trust_store_invalid",
            "trust store is not valid UTF-8 JSON",
        )
    if not isinstance(records, dict) or any(
        not _SKILL_NAME_RE.fullmatch(name) or not _record_is_valid(record)
        for name, record in records.items()
    ):
        return _unreliable(
            "trust_store_invalid",
            "trust store schema is unsupported or invalid",
        )
    return SkillTrustStoreState(
        reliable=True,
        record_present=skill_name in records,
        fingerprint=fingerprint,
    )
