"""DOCX 同目录临时文件与原子提交工具。"""

from __future__ import annotations

import errno
import os
import tempfile
from pathlib import Path

from .errors import DocxError


_UNSUPPORTED_LINK_ERRNOS = frozenset(
    value
    for value in (
        getattr(errno, "ENOSYS", None),
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "EXDEV", None),
    )
    if value is not None
)
_UNSUPPORTED_LINK_WINERRORS = frozenset({1, 50})


def create_temporary_output_path(output_path: Path) -> Path:
    """在最终输出同目录创建并保留一个普通临时文件。"""

    temporary_path: Path | None = None
    descriptor: int | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=".myhermes-docx-",
            suffix=".tmp.docx",
            dir=output_path.parent,
        )
        temporary_path = Path(raw_path).resolve()
        os.close(descriptor)
        descriptor = None
        return temporary_path
    except OSError as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        best_effort_unlink(temporary_path)
        raise DocxError("io_error", "无法在目标目录创建临时 DOCX。") from exc


def commit_with_overwrite(temporary_output: Path, output_path: Path) -> None:
    """原子替换最终输出。"""

    try:
        os.replace(temporary_output, output_path)
    except OSError as exc:
        raise DocxError("io_error", "无法原子替换目标 DOCX。") from exc


def commit_without_overwrite(temporary_output: Path, output_path: Path) -> None:
    """通过同文件系统硬链接执行原子 no-clobber 提交。"""

    if temporary_output.parent != output_path.parent:
        raise DocxError("io_error", "无覆盖提交要求临时文件与目标文件位于同一目录。")
    if not hasattr(os, "link"):
        raise DocxError("io_error", "当前平台不支持原子无覆盖 DOCX 提交。")
    try:
        os.link(temporary_output, output_path)
    except FileExistsError as exc:
        raise DocxError("output_exists", "目标 DOCX 已存在。") from exc
    except PermissionError as exc:
        raise DocxError("io_error", "没有权限以原子无覆盖方式提交目标 DOCX。") from exc
    except OSError as exc:
        if (
            exc.errno in _UNSUPPORTED_LINK_ERRNOS
            or getattr(exc, "winerror", None) in _UNSUPPORTED_LINK_WINERRORS
        ):
            raise DocxError(
                "io_error",
                "当前文件系统不支持原子无覆盖 DOCX 提交。",
            ) from exc
        raise DocxError("io_error", "无法以原子无覆盖方式提交目标 DOCX。") from exc


def best_effort_unlink(path: Path | None) -> None:
    """尽力清理临时文件，不掩盖原始异常。"""

    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
