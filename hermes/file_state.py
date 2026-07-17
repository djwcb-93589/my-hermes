"""File 审批使用的文件状态快照与复检。"""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections.abc import Mapping

from hermes.path_policy import PathAccessPolicy


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_HASH_CHUNK_SIZE = 1024 * 1024
_FILE_TYPES = frozenset({"missing", "file", "directory", "other"})


class FileStateSnapshotError(Exception):
    """无法得到内部一致的文件状态快照。"""


def normalize_file_state_snapshot(snapshot: object) -> dict:
    """严格校验并返回可稳定序列化的快照。"""
    if not isinstance(snapshot, Mapping):
        raise ValueError("file state snapshot must be a mapping")

    abs_path = snapshot.get("abs_path")
    parent_abs_path = snapshot.get("parent_abs_path")
    exists = snapshot.get("exists")
    file_type = snapshot.get("file_type")
    if not isinstance(abs_path, str) or not abs_path:
        raise ValueError("file state snapshot abs_path is invalid")
    if not isinstance(parent_abs_path, str) or not parent_abs_path:
        raise ValueError("file state snapshot parent path is invalid")
    if not isinstance(exists, bool):
        raise ValueError("file state snapshot exists is invalid")
    if file_type not in _FILE_TYPES:
        raise ValueError("file state snapshot file_type is invalid")

    raw_size = snapshot.get("size")
    raw_mtime = snapshot.get("mtime")
    raw_sha256 = snapshot.get("sha256")
    if not exists:
        if (
            file_type != "missing"
            or raw_size is not None
            or raw_mtime is not None
            or raw_sha256 is not None
        ):
            raise ValueError("missing file snapshot contains file metadata")
        size = None
        mtime = None
        sha256 = None
    else:
        if file_type == "missing":
            raise ValueError("existing file snapshot has missing file type")
        if isinstance(raw_size, bool):
            raise ValueError("file state snapshot size is invalid")
        try:
            size = int(raw_size)
            mtime = float(raw_mtime)
        except (TypeError, ValueError) as exc:
            raise ValueError("file state snapshot metadata is invalid") from exc
        if size < 0 or not math.isfinite(mtime):
            raise ValueError("file state snapshot size is invalid")
        if file_type == "file":
            if not isinstance(raw_sha256, str) or not _SHA256_RE.fullmatch(
                raw_sha256
            ):
                raise ValueError("file state snapshot sha256 is invalid")
            sha256 = raw_sha256
        else:
            if raw_sha256 is not None:
                raise ValueError("non-file snapshot cannot contain sha256")
            sha256 = None

    return {
        "abs_path": abs_path,
        "exists": exists,
        "file_type": file_type,
        "size": size,
        "mtime": mtime,
        "sha256": sha256,
        "parent_abs_path": parent_abs_path,
    }


def _snapshot_file_type(info: Mapping) -> str:
    """把 backend 的布尔类型字段收敛为稳定枚举。"""
    if info.get("is_file") is True:
        return "file"
    if info.get("is_dir") is True:
        return "directory"
    return "other"


def _hash_file(backend, abs_path: str, size: int) -> str:
    """通过 backend 分块读取完整文件并生成 SHA-256。"""
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        data = backend.read_file(
            abs_path,
            offset=offset,
            limit=min(_HASH_CHUNK_SIZE, size - offset),
        )
        if not isinstance(data, bytes) or not data:
            raise FileStateSnapshotError(
                "file changed while approval snapshot was being captured"
            )
        digest.update(data)
        offset += len(data)
    return f"sha256:{digest.hexdigest()}"


def capture_file_state_snapshot(
    backend,
    abs_path: str,
    *,
    path_policy: PathAccessPolicy,
) -> dict:
    """捕获路径、父目录和内容摘要组成的内部一致快照。"""
    normalized_path = path_policy.require_allowed(
        abs_path,
        cwd=backend.cwd,
    )
    parent_abs_path = path_policy.normalize_path(
        os.path.dirname(normalized_path) or normalized_path,
        cwd=backend.cwd,
    )
    try:
        before = backend.stat_file(normalized_path)
    except FileNotFoundError:
        return normalize_file_state_snapshot({
            "abs_path": normalized_path,
            "exists": False,
            "file_type": "missing",
            "size": None,
            "mtime": None,
            "sha256": None,
            "parent_abs_path": parent_abs_path,
        })

    file_type = _snapshot_file_type(before)
    size = int(before.get("size", 0))
    mtime = float(before.get("mtime"))
    sha256 = (
        _hash_file(backend, normalized_path, size)
        if file_type == "file"
        else None
    )

    try:
        after = backend.stat_file(normalized_path)
    except FileNotFoundError as exc:
        raise FileStateSnapshotError(
            "file changed while approval snapshot was being captured"
        ) from exc
    if (
        _snapshot_file_type(after) != file_type
        or int(after.get("size", 0)) != size
        or float(after.get("mtime")) != mtime
    ):
        raise FileStateSnapshotError(
            "file changed while approval snapshot was being captured"
        )

    return normalize_file_state_snapshot({
        "abs_path": normalized_path,
        "exists": True,
        "file_type": file_type,
        "size": size,
        "mtime": mtime,
        "sha256": sha256,
        "parent_abs_path": parent_abs_path,
    })


def file_state_snapshot_matches(
    backend,
    abs_path: str,
    snapshot: object,
    *,
    path_policy: PathAccessPolicy,
) -> bool:
    """重新捕获当前状态并与获批快照逐字段比较。"""
    approved = normalize_file_state_snapshot(snapshot)
    current = capture_file_state_snapshot(
        backend,
        abs_path,
        path_policy=path_policy,
    )
    return current == approved
