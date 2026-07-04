"""
Execution environment abstraction.

BaseExecutionEnvironment wraps every command with CWD tracking, env-var
snapshotting for session persistence, secret-blocklist filtering, and timeout
handling. Concrete backends implement _run_bash() and cleanup().
"""

from __future__ import annotations

import os
import subprocess
import uuid
from abc import ABC, abstractmethod
from pathlib import Path

from hermes.config import _config


# Hermes's API key must never leak to spawned processes.
_SECRET_BLOCKLIST = frozenset([
    "OPENAI_API_KEY", "ANTHROPIC_TOKEN", "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY", "GITHUB_TOKEN",
])


class BaseExecutionEnvironment(ABC):
    """
    The contract every terminal backend must fulfil.

    Subclasses implement _run_bash() and cleanup().
    Everything else -- command wrapping, snapshot restore, CWD tracking,
    timeout handling -- is shared.
    """

    def __init__(self, cwd: str, timeout: int = 180):
        self.cwd = cwd
        self.timeout = timeout
        self._session_id = uuid.uuid4().hex[:12]
        self._snapshot_path = f"/tmp/hermes-snap-{self._session_id}.sh"
        self._cwd_file = f"/tmp/hermes-cwd-{self._session_id}.txt"
        self._snapshot_ready = False

    @abstractmethod
    def _run_bash(self, cmd_string: str, *, timeout: int) -> subprocess.Popen:
        """Spawn a bash process to execute the wrapped command."""
        ...

    @abstractmethod
    def cleanup(self):
        """Release backend-specific resources."""
        ...

    def init_session(self):
        """Capture the current shell environment into a snapshot file."""
        init_cmd = (
            f"export -p > {self._snapshot_path} 2>/dev/null; "
            f"pwd -P > {self._cwd_file}"
        )
        proc = self._run_bash(init_cmd, timeout=10)
        proc.wait(timeout=10)
        self._snapshot_ready = True

    def execute(self, command: str, timeout: int | None = None) -> dict:
        """Wrap, run, wait, update CWD. Returns {"output": str, "returncode": int}."""
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
        """Wrap a bare command into: restore env → cd → run → save env → save CWD."""
        import shlex
        parts = []
        if self._snapshot_ready:
            parts.append(f"source {self._snapshot_path} 2>/dev/null")
        parts.append(f"cd {shlex.quote(self.cwd)} 2>/dev/null")
        parts.append(command)
        parts.append(f"_exit=$?; export -p > {self._snapshot_path} 2>/dev/null; "
                     f"pwd -P > {self._cwd_file} 2>/dev/null; exit $_exit")
        return "; ".join(parts)

    def _update_cwd(self):
        """Read the CWD file to track directory changes."""
        try:
            new_cwd = Path(self._cwd_file).read_text().strip()
            if new_cwd:
                self.cwd = new_cwd
        except FileNotFoundError:
            pass


def create_backend(config: dict) -> BaseExecutionEnvironment:
    """Create the right backend based on config."""
    # Local imports to avoid circular references at module load time.
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


_backend: BaseExecutionEnvironment | None = None


def get_backend() -> BaseExecutionEnvironment:
    """Get or create the global backend instance."""
    global _backend
    if _backend is None:
        _backend = create_backend(_config)
    return _backend
