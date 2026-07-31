"""配置文件原始字节 revision 的中立读取边界。"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path


MAX_CONFIG_REVISION_BYTES = 4 * 1024 * 1024


def revision_for_bytes(payload: bytes) -> str:
    """仅由原始字节生成稳定 SHA-256 revision。"""
    if type(payload) is not bytes:
        raise TypeError("config revision payload must be bytes")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class FileConfigRevisionReader:
    """只读取既有普通文件，不创建文件、不解析内容。"""

    __slots__ = ("_path",)

    def __init__(self, path: str | os.PathLike[str]) -> None:
        try:
            self._path = Path(path)
        except (TypeError, ValueError) as exc:
            raise TypeError("config revision path is invalid") from exc

    def read_revision(self) -> str | None:
        """文件不可读或身份发生变化时返回 unknown。"""
        descriptor = -1
        try:
            identity = self._path.lstat()
            if stat.S_ISLNK(identity.st_mode) or not stat.S_ISREG(identity.st_mode):
                return None
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self._path, flags)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or not os.path.samestat(identity, opened)
            ):
                return None
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                payload = handle.read(MAX_CONFIG_REVISION_BYTES + 1)
            if len(payload) > MAX_CONFIG_REVISION_BYTES:
                return None
            current = self._path.lstat()
            if (
                stat.S_ISLNK(current.st_mode)
                or not os.path.samestat(opened, current)
            ):
                return None
            return revision_for_bytes(payload)
        except (OSError, ValueError):
            return None
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


__all__ = [
    "MAX_CONFIG_REVISION_BYTES",
    "FileConfigRevisionReader",
    "revision_for_bytes",
]
