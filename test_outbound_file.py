"""出站文件稳定快照的跨平台回归测试。"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

from hermes.outbound_file import (
    _stat_identity,
    capture_outbound_file_snapshot,
)
from hermes.path_policy import PathAccessPolicy


def _stat(*, inode=7, ctime_ns=100):
    return SimpleNamespace(
        st_dev=3,
        st_ino=inode,
        st_size=11,
        st_mtime_ns=90,
        st_ctime_ns=ctime_ns,
    )


def test_stat_identity_can_ignore_only_cross_api_ctime_difference():
    first = _stat(ctime_ns=100)
    different_ctime = _stat(ctime_ns=101)
    different_inode = _stat(inode=8, ctime_ns=101)

    assert _stat_identity(first) != _stat_identity(different_ctime)
    assert _stat_identity(
        first,
        include_ctime=False,
    ) == _stat_identity(
        different_ctime,
        include_ctime=False,
    )
    assert _stat_identity(
        first,
        include_ctime=False,
    ) != _stat_identity(
        different_inode,
        include_ctime=False,
    )


def test_capture_outbound_snapshot_accepts_stable_local_file(tmp_path):
    content = b"stable outbound file\n"
    outbound = tmp_path / "report.docx"
    outbound.write_bytes(content)
    path_policy = PathAccessPolicy(())

    snapshot = capture_outbound_file_snapshot(
        str(outbound),
        path_policy=path_policy,
        allowed_roots=[str(tmp_path)],
        max_file_bytes=1024,
        database_path=str(tmp_path / "runtime.db"),
        sensitive_patterns=(),
    )

    assert snapshot["abs_path"] == path_policy.normalize_path(str(outbound))
    assert snapshot["size_bytes"] == len(content)
    assert snapshot["sha256"] == hashlib.sha256(content).hexdigest()
