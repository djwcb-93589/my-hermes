"""LocalBackend: execute commands on the host via subprocess."""

from __future__ import annotations

import os
import subprocess

from hermes.backends import BaseExecutionEnvironment, _SECRET_BLOCKLIST


class LocalBackend(BaseExecutionEnvironment):
    """Execute commands directly on the host via subprocess."""

    def _run_bash(self, cmd_string: str, *, timeout: int) -> subprocess.Popen:
        env = {k: v for k, v in os.environ.items() if k not in _SECRET_BLOCKLIST}
        return subprocess.Popen(
            ["bash", "-c", cmd_string],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )

    def cleanup(self):
        for path in [self._snapshot_path, self._cwd_file]:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
