"""平台无关的入站文件缓存写入与过期清理。"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO


_CACHE_FILE_PREFIX = "hermes_"
_SAFE_SUFFIXES = frozenset({
    ".bin",
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".ogg",
    ".flac",
    ".mp4",
    ".mov",
    ".avi",
    ".mkv",
    ".webm",
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
})
_MIME_SUFFIXES = {
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "application/json": ".json",
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (
        ".docx"
    ),
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (
        ".xlsx"
    ),
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": (
        ".pptx"
    ),
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
    "application/zip": ".zip",
    "application/x-rar-compressed": ".rar",
    "application/x-7z-compressed": ".7z",
    "application/x-tar": ".tar",
    "application/gzip": ".gz",
    "application/octet-stream": ".bin",
}
_WINDOWS_DEVICE_NAMES = frozenset({
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
})
_MIME_TYPE_RE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$"
)
_GENERATED_FILE_RE = re.compile(
    rf"^{_CACHE_FILE_PREFIX}[0-9a-f]{{24}}_[0-9a-f]{{32}}"
    rf"(?:{'|'.join(re.escape(item) for item in sorted(_SAFE_SUFFIXES))})"
    r"(?:\.part)?$"
)


class FileCacheError(RuntimeError):
    """缓存边界的稳定基础错误。"""

    code = "cache_error"


class FileCacheSecurityError(FileCacheError):
    """文件名或缓存路径违反安全边界。"""

    code = "unsafe_cache_path"


class FileCacheCollisionError(FileCacheError):
    """随机目标名发生冲突且不能覆盖已有文件。"""

    code = "cache_name_collision"


class FileTooLargeError(FileCacheError):
    """流式累计字节数超过配置上限。"""

    code = "file_too_large"


@dataclass(frozen=True)
class CachedFile:
    """已经完整发布到缓存目录的文件事实。"""

    local_path: str
    mime_type: str | None
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class CacheCleanupResult:
    """缓存清理结果；失败以计数返回，不向 Gateway 抛出。"""

    scanned_files: int = 0
    removed_files: int = 0
    failed_files: int = 0
    error_code: str | None = None


def normalize_mime_type(value: object) -> str | None:
    """只保留规范化的 MIME 主类型，不信任参数或控制字符。"""
    if not isinstance(value, str):
        return None
    normalized = value.split(";", 1)[0].strip().lower()
    if not _MIME_TYPE_RE.fullmatch(normalized):
        return None
    return normalized


def _validate_original_name(original_name: str) -> None:
    """拒绝路径、盘符、UNC、设备名和控制字符注入。"""
    if not original_name or not original_name.strip():
        raise FileCacheSecurityError("unsafe_original_name")
    if original_name != original_name.strip():
        raise FileCacheSecurityError("unsafe_original_name")
    if any(ord(char) < 32 for char in original_name):
        raise FileCacheSecurityError("unsafe_original_name")
    if (
        ".." in original_name
        or "/" in original_name
        or "\\" in original_name
        or original_name.startswith(("//", "\\\\"))
        or PurePosixPath(original_name).is_absolute()
        or PureWindowsPath(original_name).is_absolute()
        or bool(PureWindowsPath(original_name).drive)
        or any(char in '<>:"|?*' for char in original_name)
        or original_name.endswith((".", " "))
    ):
        raise FileCacheSecurityError("unsafe_original_name")
    device_stem = original_name.split(".", 1)[0].rstrip(" .").upper()
    if device_stem in _WINDOWS_DEVICE_NAMES:
        raise FileCacheSecurityError("unsafe_original_name")


def _safe_suffix(
    original_name: str | None,
    mime_type: str | None,
) -> str:
    """从安全原名或 MIME 白名单选后缀，无法确定时回退到 .bin。"""
    if original_name is not None:
        if not isinstance(original_name, str):
            raise FileCacheSecurityError("unsafe_original_name")
        _validate_original_name(original_name)
        suffix = Path(original_name).suffix.lower()
        if suffix in _SAFE_SUFFIXES:
            return suffix
    return _MIME_SUFFIXES.get(mime_type or "", ".bin")


def _require_direct_child(root: Path, path: Path) -> None:
    """确保模块生成的路径始终是缓存根目录的直接子项。"""
    if path.parent.resolve(strict=False) != root:
        raise FileCacheSecurityError("unsafe_cache_path")


def _publish_without_overwrite(part_path: Path, final_path: Path) -> None:
    """原子发布缓存文件，并保证已有目标不会被覆盖。"""
    if os.name == "nt":
        # Windows 的 os.rename 在目标存在时失败，满足 no-replace 语义。
        os.rename(part_path, final_path)
        return

    # POSIX rename 默认会覆盖目标；硬链接创建具有原子 no-replace 语义。
    os.link(part_path, final_path, follow_symlinks=False)
    try:
        part_path.unlink()
    except OSError:
        try:
            final_path.unlink()
        except OSError:
            pass
        raise


class _CacheWriter:
    """同目录临时写入器；只有 commit 后文件才对外可见。"""

    def __init__(
        self,
        *,
        root: Path,
        final_path: Path,
        part_path: Path,
        handle: BinaryIO,
        max_bytes: int,
        mime_type: str | None,
    ):
        self.root = root
        self.final_path = final_path
        self.part_path = part_path
        self._handle = handle
        self._max_bytes = max_bytes
        self._mime_type = mime_type
        self._size_bytes = 0
        self._sha256 = hashlib.sha256()
        self._committed = False

    def __enter__(self) -> _CacheWriter:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if not self._committed:
            self.abort()

    def write(self, chunk: bytes) -> None:
        """在写盘前检查实际累计大小，超过上限立即停止。"""
        if not isinstance(chunk, bytes):
            raise TypeError("cache chunks must be bytes")
        next_size = self._size_bytes + len(chunk)
        if next_size > self._max_bytes:
            raise FileTooLargeError("file_too_large")
        if not chunk:
            return
        self._handle.write(chunk)
        self._sha256.update(chunk)
        self._size_bytes = next_size

    def commit(self) -> CachedFile:
        """flush 并关闭临时文件，再在同目录执行原子 rename。"""
        if self._committed:
            raise FileCacheError("cache_already_committed")
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()
        _require_direct_child(self.root, self.final_path)
        _require_direct_child(self.root, self.part_path)
        if os.path.lexists(self.final_path):
            raise FileCacheCollisionError("cache_name_collision")
        try:
            _publish_without_overwrite(self.part_path, self.final_path)
        except FileExistsError as exc:
            raise FileCacheCollisionError("cache_name_collision") from exc
        self._committed = True
        return CachedFile(
            local_path=str(self.final_path),
            mime_type=self._mime_type,
            size_bytes=self._size_bytes,
            sha256=self._sha256.hexdigest(),
        )

    def abort(self) -> None:
        """尽最大努力关闭并删除临时文件，不掩盖原始异常或取消。"""
        try:
            if not self._handle.closed:
                self._handle.close()
        except OSError:
            pass
        try:
            self.part_path.unlink(missing_ok=True)
        except OSError:
            pass


class InboundFileCache:
    """只生成不可预测文件名的有界入站缓存。"""

    def __init__(self, download_dir: str | Path, max_bytes: int):
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
            raise ValueError("max_bytes must be a positive integer")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self.root = Path(download_dir).resolve(strict=False)
        self.max_bytes = max_bytes

    def open_writer(
        self,
        *,
        message_id: str,
        original_name: str | None,
        mime_type: str | None,
    ) -> _CacheWriter:
        """创建独占 .part 文件；用户文件名从不进入实际路径。"""
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            raise FileCacheSecurityError("invalid_message_id")
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.resolve(strict=False) != self.root:
            raise FileCacheSecurityError("unsafe_download_dir")
        if not self.root.is_dir():
            raise FileCacheSecurityError("unsafe_download_dir")

        normalized_mime = normalize_mime_type(mime_type)
        suffix = _safe_suffix(original_name, normalized_mime)
        message_digest = hashlib.sha256(
            normalized_message_id.encode("utf-8", errors="surrogatepass")
        ).hexdigest()[:24]

        for _ in range(32):
            random_id = uuid.uuid4().hex
            filename = (
                f"{_CACHE_FILE_PREFIX}{message_digest}_{random_id}{suffix}"
            )
            final_path = self.root / filename
            part_path = self.root / f"{filename}.part"
            _require_direct_child(self.root, final_path)
            _require_direct_child(self.root, part_path)
            if os.path.lexists(final_path):
                continue
            try:
                handle = part_path.open("xb")
            except FileExistsError:
                continue
            return _CacheWriter(
                root=self.root,
                final_path=final_path,
                part_path=part_path,
                handle=handle,
                max_bytes=self.max_bytes,
                mime_type=normalized_mime,
            )
        raise FileCacheCollisionError("cache_name_collision")


def _cleanup_expired_cache_sync(
    download_dir: str | Path,
    retention_seconds: float,
) -> CacheCleanupResult:
    """同步扫描单层缓存目录，不递归也不跟随符号链接。"""
    try:
        retention = float(retention_seconds)
    except (TypeError, ValueError, OverflowError):
        return CacheCleanupResult(
            failed_files=1,
            error_code="invalid_cache_retention",
        )
    if not math.isfinite(retention) or retention <= 0:
        return CacheCleanupResult(
            failed_files=1,
            error_code="invalid_cache_retention",
        )

    root = Path(download_dir).resolve(strict=False)
    if not root.exists():
        return CacheCleanupResult()
    if not root.is_dir():
        return CacheCleanupResult(
            failed_files=1,
            error_code="invalid_cache_directory",
        )

    scanned = 0
    removed = 0
    failed = 0
    cutoff = time.time() - retention
    try:
        with os.scandir(root) as iterator:
            entries = list(iterator)
    except OSError:
        return CacheCleanupResult(
            failed_files=1,
            error_code="cache_scan_failed",
        )

    for entry in entries:
        if not _GENERATED_FILE_RE.fullmatch(entry.name):
            continue
        scanned += 1
        try:
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                continue
            stat_result = entry.stat(follow_symlinks=False)
            if stat_result.st_mtime > cutoff:
                continue
            os.unlink(entry.path)
            removed += 1
        except OSError:
            failed += 1

    return CacheCleanupResult(
        scanned_files=scanned,
        removed_files=removed,
        failed_files=failed,
        error_code="cache_cleanup_partial" if failed else None,
    )


async def cleanup_expired_cache(
    download_dir: str | Path,
    retention_seconds: float,
) -> CacheCleanupResult:
    """在线程中清理过期文件；普通失败只进入结构化结果。"""
    try:
        return await asyncio.to_thread(
            _cleanup_expired_cache_sync,
            download_dir,
            retention_seconds,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return CacheCleanupResult(
            failed_files=1,
            error_code="cache_cleanup_failed",
        )
