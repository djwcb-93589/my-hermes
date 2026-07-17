"""
LocalBackend：通过 subprocess 在本机执行命令。

Windows 上明确优先使用 Git Bash，绝不回退到 WSL 的
``C:\\Windows\\System32\\bash.exe``。Snapshot / cwd 临时文件落在
``<HERMES_HOME>/cache/terminal/`` 下，并以两种形式跟踪（shell 形式
给 Git Bash 用，host 形式给 Python 用）。
"""

from __future__ import annotations

import os
import signal
import stat as stat_mod
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from hermes.backends import (
    BaseExecutionEnvironment,
    filter_local_subprocess_environment,
)
from hermes.path_policy import PathAccessPolicy
from hermes.path_utils import (
    git_bash_to_windows_path as _bash_to_win_path,
    windows_to_git_bash_path as _win_to_bash_path,
)


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.AssignProcessToJobObject.argtypes = (
        wintypes.HANDLE,
        wintypes.HANDLE,
    )
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL


def _attach_windows_job(proc: subprocess.Popen) -> None:
    """把进程放入独立 Job，供后续可靠终止仍存活的后代进程。"""
    if sys.platform != "win32":
        return
    job_handle = _kernel32.CreateJobObjectW(None, None)
    if not job_handle:
        return
    assigned = _kernel32.AssignProcessToJobObject(
        job_handle,
        wintypes.HANDLE(int(proc._handle)),
    )
    if not assigned:
        _kernel32.CloseHandle(job_handle)
        return
    proc._hermes_job_handle = job_handle


# ---------------------------------------------------------------------------
# Git Bash 发现（仅 Windows；POSIX 系统继续用普通的 `bash`）。
# ---------------------------------------------------------------------------

_GIT_BASH_CANDIDATES = [
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
]


def _find_git_bash() -> str:
    """在 Windows 上定位 Git Bash。返回绝对 Windows 路径。

    解析顺序：
      1. config.yaml 里的 terminal.git_bash_path
      2. 环境变量 $HERMES_GIT_BASH_PATH
      3. C:\\Program Files\\Git 下的常见安装位置

    找不到时抛 RuntimeError —— 绝不静默回退到
    ``C:\\Windows\\System32\\bash.exe``（那是 WSL）。
    """
    from hermes.config import _config

    cfg_path = _config.get("terminal", {}).get("git_bash_path")
    if cfg_path and Path(cfg_path).is_file():
        return cfg_path

    env_path = os.environ.get("HERMES_GIT_BASH_PATH")
    if env_path and Path(env_path).is_file():
        return env_path

    for cand in _GIT_BASH_CANDIDATES:
        if Path(cand).is_file():
            return cand

    raise RuntimeError(
        "Git Bash not found. Set `terminal.git_bash_path` in config.yaml "
        "or HERMES_GIT_BASH_PATH env var to the full path of "
        "'C:\\Program Files\\Git\\bin\\bash.exe'. "
        "Hermes refuses to fall back to WSL bash."
    )


# ---------------------------------------------------------------------------
# LocalBackend。
# ---------------------------------------------------------------------------

class LocalBackend(BaseExecutionEnvironment):
    """通过 subprocess 在本机执行命令。

    Windows 上使用 Git Bash（绝不用 WSL）。临时文件放在
    HERMES_HOME/cache/terminal/ 下。cwd 内部以 Windows 形式保存，注入
    bash 命令时转成 MSYS 形式。
    """

    terminal_path_preflight_enabled = True
    backend_type = "local"

    def __init__(
        self,
        cwd: str,
        timeout: int = 180,
        *,
        path_policy: PathAccessPolicy | None = None,
        tool_approval_policy=None,
        env_passthrough: Iterable[str] = (),
        infrastructure_secret_values: Iterable[str] = (),
    ):
        self._env_passthrough = frozenset(env_passthrough)
        self._infrastructure_secret_values = frozenset(
            infrastructure_secret_values
        )
        super().__init__(
            cwd=cwd,
            timeout=timeout,
            path_policy=path_policy,
            tool_approval_policy=tool_approval_policy,
        )

    def _setup_paths(self):
        """Windows 下把 snapshot/cwd 文件迁到 HERMES_HOME/cache/terminal/。

        POSIX 系统沿用基类的 /tmp 默认值。
        """
        if sys.platform != "win32":
            return
        from hermes.config import HERMES_HOME

        tmp_dir = HERMES_HOME / "cache" / "terminal"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        snap_host = tmp_dir / f"hermes-snap-{self._session_id}.sh"
        cwd_host = tmp_dir / f"hermes-cwd-{self._session_id}.txt"

        self._snapshot_host = str(snap_host)
        self._cwd_host = str(cwd_host)
        self._snapshot_shell = _win_to_bash_path(self._snapshot_host)
        self._cwd_shell = _win_to_bash_path(self._cwd_host)

    def _cwd_to_shell(self, cwd: str) -> str:
        if sys.platform == "win32":
            return _win_to_bash_path(cwd)
        return cwd

    def _normalize_cwd(self, raw: str) -> str:
        if sys.platform == "win32" and raw.startswith("/") and len(raw) >= 3 and raw[2] == "/":
            return _bash_to_win_path(raw)
        return raw

    def _run_bash(self, cmd_string: str, *, timeout: int) -> subprocess.Popen:
        # 解析 bash 可执行文件。Windows 上必须显式指定 ——
        # `["bash", ...]` 会沿 PATH 查找，可能命中 WSL 的 bash。
        bash = _find_git_bash() if sys.platform == "win32" else "bash"

        env = filter_local_subprocess_environment(
            os.environ,
            env_passthrough=self._env_passthrough,
            infrastructure_secret_values=self._infrastructure_secret_values,
        )
        process_group_options = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if sys.platform == "win32"
            else {"start_new_session": True}
        )
        proc = subprocess.Popen(
            [bash, "-c", cmd_string],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",   # bash 输出 UTF-8；不让 Windows 用 GBK 解码
            errors="replace",
            env=env,
            **process_group_options,
        )
        _attach_windows_job(proc)
        return proc

    def _interrupt_process(self, proc: subprocess.Popen) -> None:
        """向整组进程发送与终端 Ctrl+C 等价的软中断。"""
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
            return
        os.killpg(proc.pid, signal.SIGINT)

    def _kill_process_tree(self, proc: subprocess.Popen) -> None:
        """软中断无效时强制结束本地命令的进程树。"""
        if sys.platform == "win32":
            job_handle = getattr(proc, "_hermes_job_handle", None)
            if job_handle and _kernel32.TerminateJobObject(job_handle, 130):
                return
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            if proc.poll() is None:
                proc.kill()
            return

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _release_process_resources(self, proc: subprocess.Popen) -> None:
        """关闭 Windows Job 句柄；正常结束时不终止后台子进程。"""
        if sys.platform != "win32":
            return
        job_handle = getattr(proc, "_hermes_job_handle", None)
        if not job_handle:
            return
        _kernel32.CloseHandle(job_handle)
        proc._hermes_job_handle = None

    def cleanup(self):
        """通过 HOST 路径删除 snapshot/cwd 文件。"""
        for path in [self._snapshot_host, self._cwd_host]:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    # --- 文件 IO：LocalBackend 直接走 Python 标准库 ---

    def resolve_path(self, rel_path: str) -> str:
        """相对路径以 cwd 为基准；同时接受 MSYS 形式（``/d/...``）方便 LLM。"""
        rel_path = os.path.expandvars(os.path.expanduser(rel_path))
        if (
            sys.platform == "win32"
            and rel_path.startswith("/")
            and len(rel_path) >= 2
            and rel_path[1].isalpha()
            and (len(rel_path) == 2 or rel_path[2] == "/")
        ):
            rel_path = _bash_to_win_path(rel_path)
        return super().resolve_path(rel_path)

    def read_file(self, path: str, offset: int = 0, limit: int | None = None) -> bytes:
        """按字节读取文件，offset/limit 都是字节单位。"""
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(limit) if limit is not None else f.read()

    def write_file(self, path: str, content: bytes, mode: str = "write") -> None:
        """写入文件。write 模式走 tmp + os.replace 原子替换；append 直接追加。"""
        # 父目录不存在时自动创建，对齐旧 write_file 的行为。
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        if mode == "append":
            with open(path, "ab") as f:
                f.write(content)
            return

        tmp = path + ".hermes.tmp"
        with open(tmp, "wb") as f:
            f.write(content)
        os.replace(tmp, path)

    def list_dir(self, path: str) -> list[str]:
        """返回目录下条目名（不含路径前缀）。"""
        return [entry.name for entry in os.scandir(path)]

    def stat_file(self, path: str) -> dict:
        """返回文件元数据。"""
        st = os.stat(path)
        return {
            "size": st.st_size,
            "is_dir": stat_mod.S_ISDIR(st.st_mode),
            "is_file": stat_mod.S_ISREG(st.st_mode),
            "mtime": st.st_mtime,
        }
