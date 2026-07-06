"""SSHBackend：通过 SSH ControlMaster 在远端机器上执行命令。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from hermes.backends import BaseExecutionEnvironment


class SSHBackend(BaseExecutionEnvironment):
    """通过 SSH ControlMaster 在远端机器上执行命令。"""

    def __init__(self, host: str, user: str, key_path: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self._host = host
        self._user = user
        self._key_path = key_path
        ctrl_dir = Path("/tmp/hermes-ssh")
        ctrl_dir.mkdir(exist_ok=True)
        self._control_socket = str(ctrl_dir / f"{user}@{host}.sock")

    def _ssh_args(self) -> list[str]:
        args = [
            "ssh",
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={self._control_socket}",
            "-o", "ControlPersist=300",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
        ]
        if self._key_path:
            args += ["-i", self._key_path]
        args.append(f"{self._user}@{self._host}")
        return args

    def _run_bash(self, cmd_string: str, *, timeout: int) -> subprocess.Popen:
        import shlex
        return subprocess.Popen(
            self._ssh_args() + ["bash", "-c", shlex.quote(cmd_string)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def _update_cwd(self):
        """SSH：通过 ssh cat 读取远端的 CWD 文件。"""
        result = subprocess.run(
            self._ssh_args() + ["cat", self._cwd_shell],
            capture_output=True, text=True, timeout=5,
        )
        new_cwd = result.stdout.strip()
        if new_cwd:
            self.cwd = new_cwd

    def cleanup(self):
        subprocess.run(
            ["ssh", "-O", "exit",
             "-o", f"ControlPath={self._control_socket}",
             f"{self._user}@{self._host}"],
            capture_output=True,
        )
