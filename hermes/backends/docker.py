"""DockerBackend: execute commands inside a Docker container."""

from __future__ import annotations

import subprocess

from hermes.backends import BaseExecutionEnvironment


class DockerBackend(BaseExecutionEnvironment):
    """Execute commands inside a Docker container."""

    def __init__(self, image: str = "python:3.11-slim", **kwargs):
        super().__init__(**kwargs)
        self._image = image
        self._container_id: str | None = None

    def _ensure_container(self):
        """Start a long-lived container (once)."""
        if self._container_id:
            return
        result = subprocess.run(
            [
                "docker", "run", "-d",
                "--name", f"hermes-{self._session_id}",
                "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges",
                "--pids-limit", "256",
                "--cpus", "1",
                "--memory", "512m",
                "--tmpfs", "/tmp:rw,nosuid,size=256m",
                self._image,
                "sleep", "infinity",
            ],
            capture_output=True, text=True,
        )
        self._container_id = result.stdout.strip()
        if not self._container_id:
            raise RuntimeError(f"Docker start failed: {result.stderr}")

    def _run_bash(self, cmd_string: str, *, timeout: int) -> subprocess.Popen:
        self._ensure_container()
        return subprocess.Popen(
            ["docker", "exec", "-i", self._container_id, "bash", "-c", cmd_string],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def _update_cwd(self):
        """Docker: CWD file is inside the container; read via docker exec."""
        if not self._container_id:
            return
        result = subprocess.run(
            ["docker", "exec", self._container_id, "cat", self._cwd_file],
            capture_output=True, text=True,
        )
        new_cwd = result.stdout.strip()
        if new_cwd:
            self.cwd = new_cwd

    def cleanup(self):
        if self._container_id:
            subprocess.run(["docker", "rm", "-f", self._container_id],
                           capture_output=True)
            self._container_id = None
