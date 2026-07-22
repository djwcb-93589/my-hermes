"""跨模块共享的文件锁与原子写入工具。

memory / skill / skill_security 三个模块原本各自复制了一份几乎相同的
锁实现和原子写实现。本模块统一提供这两个原语,让它们只维护一份逻辑。
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path


# 锁等待与轮询参数。调用方有特殊需求时可覆盖,否则共用默认值。
DEFAULT_LOCK_TIMEOUT = 5.0
DEFAULT_LOCK_POLL = 0.05


class LockTimeout(Exception):
    """文件锁等待超时。"""


def _lock_path_for(file_path: Path) -> Path:
    """获取与目标文件同目录、同前缀的 .lock 伴生路径。"""
    return file_path.with_suffix(file_path.suffix + ".lock")


def _acquire_lock(lock_path: Path, timeout: float, poll: float) -> int:
    """O_CREAT | O_EXCL 实现跨进程互斥;超时抛 LockTimeout。"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise LockTimeout()
            time.sleep(poll)


def _release_lock(lock_path: Path, fd: int) -> None:
    """先关 fd 再删 lock 文件;删失败视作已释放,不阻塞调用方。"""
    try:
        os.close(fd)
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def file_lock(
    file_path: Path,
    *,
    timeout: float = DEFAULT_LOCK_TIMEOUT,
    poll: float = DEFAULT_LOCK_POLL,
):
    """对目标文件加跨进程锁;退出上下文时自动释放。

    锁文件路径为 ``<file>.lock``。同目录下若已有同名锁文件,会轮询等待
    直到超时,超时抛 ``LockTimeout``。
    """
    lock_path = _lock_path_for(file_path)
    fd = _acquire_lock(lock_path, timeout, poll)
    try:
        yield
    finally:
        _release_lock(lock_path, fd)


def atomic_write_text(
    file_path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
) -> None:
    """同目录写临时文件 -> flush/fsync -> os.replace 原子替换。

    异常时清理临时文件并重新抛出,旧文件保持不变。
    """
    tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    try:
        with open(tmp_path, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, file_path)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise
