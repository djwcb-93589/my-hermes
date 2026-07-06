"""
执行环境抽象。

BaseExecutionEnvironment 把每条命令包装成：CWD 追踪、环境变量快照（用于
session 间持久化）、密钥脱敏、超时处理。具体后端只需实现 _run_bash()
和 cleanup()。

路径双重表示
------------
snapshot 和 cwd 临时文件需要两种路径形式：

  - ``_snapshot_shell`` / ``_cwd_shell``  —— 嵌入 bash 命令字符串里
    的形式（如 ``source /c/Users/.../snap.sh``）。
  - ``_snapshot_host``  / ``_cwd_host``   —— Python 用来 read/unlink
    同一个文件的形式（如 ``C:\\Users\\...\\snap.sh``）。

POSIX 上两者完全相同。Windows + Git Bash 上不同 —— LocalBackend 通过
覆盖 ``_setup_paths`` 来填充全部四个变量；``_cwd_to_shell`` 和
``_normalize_cwd`` 这两个 hook 负责 cwd 在两种形式间互转。
"""

from __future__ import annotations

import os
import subprocess
import threading
import uuid
from abc import ABC, abstractmethod
from pathlib import Path

from hermes.config import _config


# Hermes 自己的 API key 绝对不能泄漏给被拉起的子进程。
_SECRET_BLOCKLIST = frozenset([
    "OPENAI_API_KEY", "ANTHROPIC_TOKEN", "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY", "GITHUB_TOKEN",
])


class UnsupportedBackendError(Exception):
    """后端未实现该文件操作（如 Docker/SSH 默认不暴露文件 IO）。"""


class BaseExecutionEnvironment(ABC):
    """
    所有 terminal 后端必须满足的契约。

    子类只需实现 _run_bash() 和 cleanup()。其它——命令包装、快照恢复、
    CWD 追踪、超时处理——都在基类里共享。
    """

    def __init__(self, cwd: str, timeout: int = 180):
        self.cwd = cwd
        self.file_root = cwd
        self.timeout = timeout
        self._session_id = uuid.uuid4().hex[:12]
        # 默认：/tmp/hermes-* （POSIX 下 shell == host）。Windows 上的
        # 子类通过覆盖 _setup_paths 把 shell / host 两种形式分开。
        stem = f"/tmp/hermes-{self._session_id}"
        self._snapshot_shell = f"{stem}-snap.sh"
        self._snapshot_host = self._snapshot_shell
        self._cwd_shell = f"{stem}-cwd.txt"
        self._cwd_host = self._cwd_shell
        self._snapshot_ready = False
        # 子类 hook：重新定位临时文件。
        self._setup_paths()

    @abstractmethod
    def _run_bash(self, cmd_string: str, *, timeout: int) -> subprocess.Popen:
        """启动一个 bash 进程执行包装好的命令。"""
        ...

    @abstractmethod
    def cleanup(self):
        """释放后端专属资源。"""
        ...

    # --- 子类 hook ---

    def _setup_paths(self):
        """覆盖此方法以重新定位 snapshot/cwd 文件。需要同时设置四个变量：
        _snapshot_shell、_snapshot_host、_cwd_shell、_cwd_host。"""
        pass

    def _cwd_to_shell(self, cwd: str) -> str:
        """把 host 形式的 cwd 转成 bash 能识别的形式。"""
        return cwd

    def _normalize_cwd(self, raw: str) -> str:
        """把 `pwd -P` 的原始输出转回 host 形式。"""
        return raw

    # --- 共享逻辑 ---

    def init_session(self):
        """把当前 shell 环境捕获到 snapshot 文件里。"""
        init_cmd = (
            f"export -p > {self._snapshot_shell} 2>/dev/null; "
            f"pwd -P > {self._cwd_shell}"
        )
        proc = self._run_bash(init_cmd, timeout=10)
        proc.wait(timeout=10)
        self._snapshot_ready = True

    def execute(self, command: str, timeout: int | None = None) -> dict:
        """包装 → 执行 → 等待 → 更新 CWD。返回 {"output": str, "returncode": int}。"""
        if not self._snapshot_ready:
            self.init_session()

        timeout = timeout or self.timeout
        wrapped = self._wrap_command(command)
        proc = self._run_bash(wrapped, timeout=timeout)

        try:
            stdout, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return {"output": "(timed out)", "returncode": 124}

        self._update_cwd()

        output = stdout or ""
        return {"output": output[:10000], "returncode": proc.returncode or 0}

    def _wrap_command(self, command: str) -> str:
        """把裸命令包装成：恢复环境 → cd → 执行 → 保存环境 → 保存 CWD。"""
        import shlex
        parts = []
        if self._snapshot_ready:
            parts.append(f"source {self._snapshot_shell} 2>/dev/null")
        cwd_for_shell = self._cwd_to_shell(self.cwd)
        parts.append(f"cd {shlex.quote(cwd_for_shell)} 2>/dev/null")
        parts.append(command)
        parts.append(f"_exit=$?; export -p > {self._snapshot_shell} 2>/dev/null; "
                     f"pwd -P > {self._cwd_shell} 2>/dev/null; exit $_exit")
        return "; ".join(parts)

    def _update_cwd(self):
        """读取 CWD 文件，跟踪目录变化。"""
        try:
            new_cwd = Path(self._cwd_host).read_text(encoding="utf-8").strip()
            if new_cwd:
                self.cwd = self._normalize_cwd(new_cwd)
        except FileNotFoundError:
            pass

    # --- 文件 IO 抽象（terminal 工具之外的文件操作走这一组） ---

    def resolve_path(self, rel_path: str) -> str:
        """把（可能是相对的）路径解析成 host 形式的绝对路径。

        相对路径以 ``self.cwd`` 为基准。子类可覆盖以处理路径形式转换
        （如 Windows + Git Bash 把 MSYS 形式 ``/d/...`` 转成 Windows 形式）。
        """
        p = Path(rel_path)
        if not p.is_absolute():
            p = Path(self.cwd) / p
        return str(p)

    def read_file(self, path: str, offset: int = 0, limit: int | None = None) -> bytes:
        """读取文件二进制内容。默认不支持，子类按需覆盖。"""
        raise UnsupportedBackendError(
            f"{type(self).__name__} does not implement read_file"
        )

    def write_file(self, path: str, content: bytes, mode: str = "write") -> None:
        """写入文件。mode: "write" 覆盖，"append" 追加。默认不支持。"""
        raise UnsupportedBackendError(
            f"{type(self).__name__} does not implement write_file"
        )

    def list_dir(self, path: str) -> list[str]:
        """列目录，返回条目名列表。默认不支持。"""
        raise UnsupportedBackendError(
            f"{type(self).__name__} does not implement list_dir"
        )

    def stat_file(self, path: str) -> dict:
        """返回文件信息 {size, is_dir, is_file, mtime}。默认不支持。"""
        raise UnsupportedBackendError(
            f"{type(self).__name__} does not implement stat_file"
        )


def create_backend(config: dict) -> BaseExecutionEnvironment:
    """根据 config 选择合适的后端。"""
    # 局部 import，避免模块加载阶段产生循环引用。
    from hermes.backends.local import LocalBackend
    from hermes.backends.docker import DockerBackend
    from hermes.backends.ssh import SSHBackend

    backend_type = config.get("terminal", {}).get("backend", "local")
    terminal_cfg = config.get("terminal", {})

    if backend_type == "docker":
        image = terminal_cfg.get("docker_image", "python:3.11-slim")
        return DockerBackend(image=image, cwd="/workspace")
    elif backend_type == "ssh":
        return SSHBackend(
            host=terminal_cfg["ssh_host"],
            user=terminal_cfg["ssh_user"],
            key_path=terminal_cfg.get("ssh_key"),
            cwd="~",
        )
    else:
        return LocalBackend(cwd=os.getcwd())


# ---------------------------------------------------------------------------
# 按 session 管理的 backend 注册表。
#
# 每个 conversation session 拿到自己独立的 backend 实例，cwd、环境快照
# 等状态不会跨 session 泄漏。session_key 由调用方用来标识一个会话：
# CLI 用 SQLite session 的 UUID；gateway 用平台维度的 session_key
# （例如 "agent:main:console:dm:console_user"）。
# ---------------------------------------------------------------------------

_backends: dict[str, BaseExecutionEnvironment] = {}
_backends_lock = threading.Lock()


def get_backend(session_key: str = "default") -> BaseExecutionEnvironment:
    """按 session_key 取或建 backend。

    同一个 session_key 的第一次调用会通过 create_backend(_config) 新建；
    后续调用直接返回缓存实例。线程安全。
    """
    with _backends_lock:
        b = _backends.get(session_key)
        if b is None:
            b = create_backend(_config)
            _backends[session_key] = b
        return b


def cleanup_backend(session_key: str) -> bool:
    """清理指定 session 的 backend。存在则返回 True。"""
    with _backends_lock:
        b = _backends.pop(session_key, None)
    if b is None:
        return False
    try:
        b.cleanup()
    except Exception:
        pass
    return True


def cleanup_all_backends() -> None:
    """清理所有缓存的 backend。程序退出时调用。"""
    with _backends_lock:
        items = list(_backends.values())
        _backends.clear()
    for b in items:
        try:
            b.cleanup()
        except Exception:
            pass
