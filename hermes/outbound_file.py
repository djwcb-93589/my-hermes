"""出站文件的本地路径校验与稳定快照。"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from typing import Iterable, Mapping, TypedDict

from hermes.path_policy import PathAccessDeniedError, PathAccessPolicy


class OutboundFileSnapshot(TypedDict):
    """审批和任务创建共同绑定的 JSON 兼容文件快照。"""

    abs_path: str
    size_bytes: int
    sha256: str
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int


class OutboundFileValidationError(ValueError):
    """携带稳定错误码的出站文件校验失败。"""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


_KEY_FILE_NAMES = frozenset({
    "authorized_keys",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "known_hosts",
})
_KEY_FILE_SUFFIXES = frozenset({".key", ".pem", ".p12", ".pfx"})
_DATABASE_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3"})
_DATABASE_RUNTIME_SUFFIXES = ("-wal", "-shm", "-journal")
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")


def _path_is_under(path: str, root: str) -> bool:
    """按平台路径规则判断文件是否位于给定根目录。"""
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def _normalize_allowed_roots(
    roots: object,
    *,
    path_policy: PathAccessPolicy,
) -> tuple[str, ...]:
    """把 Runner 传入的已解析根目录再次收敛为真实绝对路径。"""
    if not isinstance(roots, list) or not roots:
        raise OutboundFileValidationError(
            "outbound_roots_unavailable",
            "no outbound file roots are configured",
        )
    normalized: list[str] = []
    for root in roots:
        if not isinstance(root, str) or not root.strip():
            raise OutboundFileValidationError(
                "outbound_roots_invalid",
                "outbound file roots are invalid",
            )
        try:
            value = path_policy.require_allowed(root)
        except (PathAccessDeniedError, OSError, ValueError) as exc:
            raise OutboundFileValidationError(
                "path_policy_denied",
                "outbound file root is blocked by filesystem policy",
            ) from exc
        if value not in normalized:
            normalized.append(value)
    return tuple(normalized)


def normalize_display_name(value: object, *, fallback: str) -> str:
    """校验平台展示名，禁止把路径或控制字符伪装成文件名。"""
    if value is None:
        value = fallback
    if not isinstance(value, str) or not value.strip():
        raise OutboundFileValidationError(
            "invalid_display_name",
            "display_name must be a non-empty string when provided",
        )
    name = value.strip()
    drive, _ = os.path.splitdrive(name)
    if (
        drive
        or os.path.isabs(name)
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or _CONTROL_CHARACTER_RE.search(name)
    ):
        raise OutboundFileValidationError(
            "invalid_display_name",
            "display_name must be a plain file name",
        )
    return name


def _is_sensitive_path(
    abs_path: str,
    *,
    database_path: str,
    sensitive_patterns: Iterable[re.Pattern[str]],
) -> bool:
    """拒绝配置、凭证、密钥及数据库文件。"""
    normalized = abs_path.replace("\\", "/").lower()
    name = os.path.basename(abs_path).lower()
    suffix = os.path.splitext(name)[1]
    if name == "config.yaml" or name == ".env" or name.startswith(".env."):
        return True
    if name in _KEY_FILE_NAMES or suffix in _KEY_FILE_SUFFIXES:
        return True
    if suffix in _DATABASE_SUFFIXES or name.endswith(_DATABASE_RUNTIME_SUFFIXES):
        return True
    try:
        normalized_db = os.path.normcase(os.path.realpath(database_path))
    except (OSError, ValueError):
        normalized_db = ""
    if normalized_db and (
        abs_path == normalized_db
        or abs_path in {
            f"{normalized_db}-wal",
            f"{normalized_db}-shm",
            f"{normalized_db}-journal",
        }
    ):
        return True
    return any(pattern.search(normalized) for pattern in sensitive_patterns)


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    """提取读文件前后必须保持一致的状态字段。"""
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def _opened_descriptor_path(descriptor: int, fallback: str) -> str:
    """尽量从已打开描述符反查真实目标，收紧目录级符号链接竞争。"""
    proc_path = f"/proc/self/fd/{descriptor}"
    try:
        candidate = proc_path if os.path.exists(proc_path) else fallback
        return os.path.normcase(os.path.realpath(candidate))
    except (OSError, ValueError):
        return os.path.normcase(os.path.realpath(fallback))


def capture_outbound_file_snapshot(
    path: object,
    *,
    path_policy: PathAccessPolicy,
    allowed_roots: object,
    max_file_bytes: int,
    database_path: str,
    sensitive_patterns: Iterable[re.Pattern[str]],
) -> OutboundFileSnapshot:
    """流式计算文件摘要，并把路径、内容和 stat 身份绑定为稳定快照。"""
    if not isinstance(path, str) or not path.strip():
        raise OutboundFileValidationError(
            "invalid_path",
            "path must be a non-empty string",
        )
    try:
        # denied_paths 是第一道策略，审批不能越过它。
        abs_path = path_policy.require_allowed(path)
    except PathAccessDeniedError as exc:
        raise OutboundFileValidationError(
            "path_policy_denied",
            "path is blocked by the configured filesystem policy",
        ) from exc
    except (OSError, ValueError) as exc:
        raise OutboundFileValidationError(
            "invalid_path",
            "path could not be normalized",
        ) from exc

    roots = _normalize_allowed_roots(
        allowed_roots,
        path_policy=path_policy,
    )
    if not any(_path_is_under(abs_path, root) for root in roots):
        raise OutboundFileValidationError(
            "outbound_root_denied",
            "path is outside gateway.file_transfer.outbound_allowed_roots",
        )
    if _is_sensitive_path(
        abs_path,
        database_path=database_path,
        sensitive_patterns=sensitive_patterns,
    ):
        raise OutboundFileValidationError(
            "sensitive_file_denied",
            "sensitive files cannot be sent through Gateway",
        )
    if isinstance(max_file_bytes, bool):
        raise OutboundFileValidationError(
            "invalid_outbound_limit",
            "outbound file size limit is invalid",
        )
    try:
        size_limit = int(max_file_bytes)
    except (TypeError, ValueError, OverflowError) as exc:
        raise OutboundFileValidationError(
            "invalid_outbound_limit",
            "outbound file size limit is invalid",
        ) from exc
    if size_limit <= 0:
        raise OutboundFileValidationError(
            "invalid_outbound_limit",
            "outbound file size limit is invalid",
        )

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(abs_path, flags)
    except FileNotFoundError as exc:
        raise OutboundFileValidationError(
            "file_not_found",
            "outbound file does not exist",
        ) from exc
    except OSError as exc:
        raise OutboundFileValidationError(
            "file_open_failed",
            "outbound file could not be opened safely",
        ) from exc

    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OutboundFileValidationError(
                "not_regular_file",
                "outbound path must be a regular file",
            )
        opened_path = _opened_descriptor_path(descriptor, abs_path)
        if not any(_path_is_under(opened_path, root) for root in roots):
            raise OutboundFileValidationError(
                "outbound_root_denied",
                "opened file is outside outbound allowed roots",
            )
        if _is_sensitive_path(
            opened_path,
            database_path=database_path,
            sensitive_patterns=sensitive_patterns,
        ):
            raise OutboundFileValidationError(
                "sensitive_file_denied",
                "sensitive files cannot be sent through Gateway",
            )
        if before.st_size <= 0:
            raise OutboundFileValidationError(
                "empty_file_denied",
                "outbound file must not be empty",
            )
        if before.st_size > size_limit:
            raise OutboundFileValidationError(
                "file_too_large",
                "outbound file exceeds the configured size limit",
            )

        digest = hashlib.sha256()
        bytes_read = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            bytes_read += len(chunk)
            if bytes_read > size_limit:
                raise OutboundFileValidationError(
                    "file_too_large",
                    "outbound file exceeds the configured size limit",
                )
            digest.update(chunk)
        after = os.fstat(descriptor)
        if bytes_read != before.st_size or _stat_identity(before) != _stat_identity(after):
            raise OutboundFileValidationError(
                "file_changed_during_validation",
                "outbound file changed while it was being validated",
            )
    except OutboundFileValidationError:
        raise
    except OSError as exc:
        raise OutboundFileValidationError(
            "file_read_failed",
            "outbound file could not be read safely",
        ) from exc
    finally:
        os.close(descriptor)

    try:
        current = os.stat(abs_path, follow_symlinks=False)
    except OSError as exc:
        raise OutboundFileValidationError(
            "file_changed_during_validation",
            "outbound file changed after it was validated",
        ) from exc
    if not stat.S_ISREG(current.st_mode) or _stat_identity(current) != _stat_identity(after):
        raise OutboundFileValidationError(
            "file_changed_during_validation",
            "outbound file changed after it was validated",
        )

    return OutboundFileSnapshot(
        abs_path=abs_path,
        size_bytes=int(after.st_size),
        sha256=digest.hexdigest(),
        device=int(after.st_dev),
        inode=int(after.st_ino),
        mtime_ns=int(after.st_mtime_ns),
        ctime_ns=int(after.st_ctime_ns),
    )


def normalize_outbound_file_snapshot(value: object) -> OutboundFileSnapshot:
    """校验从审批详情恢复的出站文件快照。"""
    if not isinstance(value, Mapping):
        raise ValueError("outbound file snapshot must be an object")
    abs_path = value.get("abs_path")
    sha256 = value.get("sha256")
    if not isinstance(abs_path, str) or not abs_path:
        raise ValueError("outbound file snapshot path is invalid")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("outbound file snapshot sha256 is invalid")
    normalized: dict[str, object] = {
        "abs_path": abs_path,
        "sha256": sha256,
    }
    for field in ("size_bytes", "device", "inode", "mtime_ns", "ctime_ns"):
        raw = value.get(field)
        if isinstance(raw, bool):
            raise ValueError(f"outbound file snapshot {field} is invalid")
        try:
            parsed = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"outbound file snapshot {field} is invalid"
            ) from exc
        if parsed < 0 or (field == "size_bytes" and parsed <= 0):
            raise ValueError(f"outbound file snapshot {field} is invalid")
        normalized[field] = parsed
    return OutboundFileSnapshot(**normalized)
