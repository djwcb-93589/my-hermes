"""
LocalBackend：通过 subprocess 在本机执行命令。

Windows 上明确优先使用 Git Bash，绝不回退到 WSL 的
``C:\\Windows\\System32\\bash.exe``。Snapshot / cwd 临时文件落在
``<HERMES_HOME>/cache/terminal/`` 下，并以两种形式跟踪（shell 形式
给 Git Bash 用，host 形式给 Python 用）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from hermes.backends import BaseExecutionEnvironment, _SECRET_BLOCKLIST


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
# Windows ↔ Git Bash 路径互转。
# ---------------------------------------------------------------------------

def _win_to_bash_path(win_path: str) -> str:
    """``D:\\my-hermes\\foo`` → ``/d/my-hermes/foo``。

    盘符变成 ``/<letter>/``；反斜杠换成正斜杠。相对路径或 UNC 路径
    仅翻转反斜杠。
    """
    p = Path(win_path)
    if p.drive:
        drive_letter = p.drive[0].lower()
        rest = str(p)[len(p.drive):].lstrip("\\/").replace("\\", "/")
        return f"/{drive_letter}/{rest}" if rest else f"/{drive_letter}"
    return str(p).replace("\\", "/")


def _bash_to_win_path(bash_path: str) -> str:
    """``/d/my-hermes/foo`` → ``D:\\my-hermes\\foo``。

    只转换 ``/<drive-letter>/...`` 这种 MSYS 形式；其它路径（相对路径、
    ``//unc/...`` 等）原样返回。
    """
    if len(bash_path) >= 3 and bash_path[0] == "/" and bash_path[2] == "/":
        drive = bash_path[1].upper()
        rest = bash_path[3:].replace("/", "\\")
        return f"{drive}:\\{rest}"
    return bash_path


# ---------------------------------------------------------------------------
# LocalBackend。
# ---------------------------------------------------------------------------

class LocalBackend(BaseExecutionEnvironment):
    """通过 subprocess 在本机执行命令。

    Windows 上使用 Git Bash（绝不用 WSL）。临时文件放在
    HERMES_HOME/cache/terminal/ 下。cwd 内部以 Windows 形式保存，注入
    bash 命令时转成 MSYS 形式。
    """

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

        env = {k: v for k, v in os.environ.items() if k not in _SECRET_BLOCKLIST}
        return subprocess.Popen(
            [bash, "-c", cmd_string],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",   # bash 输出 UTF-8；不让 Windows 用 GBK 解码
            errors="replace",
            env=env,
        )

    def cleanup(self):
        """通过 HOST 路径删除 snapshot/cwd 文件。"""
        for path in [self._snapshot_host, self._cwd_host]:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
