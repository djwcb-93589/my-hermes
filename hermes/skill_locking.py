"""Skill 写操作共享的跨进程锁。"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path
from typing import Iterator

from hermes._io_utils import DEFAULT_LOCK_POLL, DEFAULT_LOCK_TIMEOUT, file_lock


_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def skill_lock_target(skills_root: Path, name: str) -> Path:
    """返回统一锁目标；底层会创建 ``.locks/<name>.lock``。"""

    if not isinstance(name, str) or not _SKILL_NAME_RE.fullmatch(name):
        raise ValueError("skill name must match [A-Za-z0-9_-]+")
    return Path(skills_root) / ".locks" / name


@contextlib.contextmanager
def acquire_skill_lock(
    skills_root: Path,
    name: str,
    *,
    timeout: float = DEFAULT_LOCK_TIMEOUT,
    poll: float = DEFAULT_LOCK_POLL,
) -> Iterator[None]:
    """使用所有 Skill 写操作共享的锁身份保护完整临界区。"""

    lock_target = skill_lock_target(skills_root, name)
    lock_target.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(lock_target, timeout=timeout, poll=poll):
        yield
