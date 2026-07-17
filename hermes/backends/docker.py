"""DockerBackend：在 Docker 容器内执行命令。"""

from __future__ import annotations

import os
import posixpath
import subprocess
from collections.abc import Mapping, Sequence

from hermes.backends import BaseExecutionEnvironment
from hermes.path_policy import PathAccessDeniedError


class DockerBackend(BaseExecutionEnvironment):
    """在 Docker 容器内执行命令。"""

    backend_type = "docker"

    def __init__(
        self,
        image: str = "python:3.11-slim",
        *,
        mounts: Sequence[Mapping] = (),
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._image = image
        self._container_id: str | None = None
        normalized_mounts: list[dict] = []
        hardline_targets: list[str] = []
        configured_targets: list[str] = []
        docker_socket = False

        def map_protected_target(
            protected: str,
            source: str,
            target: str,
        ) -> str:
            """把宿主受保护路径精确投影到容器挂载目标。"""
            relative = os.path.relpath(protected, source)
            if relative == "." or relative.startswith(f"..{os.sep}"):
                return posixpath.normpath(target)
            return posixpath.normpath(posixpath.join(
                target,
                relative.replace("\\", "/"),
            ))

        for mount in mounts:
            source = str(mount["source"])
            target = str(mount["target"])
            source_key = source.replace("\\", "/").casefold()
            is_socket = (
                source_key.endswith("/docker.sock")
                or source_key == "//./pipe/docker_engine"
            )
            normalized_source = (
                source
                if is_socket
                else os.path.realpath(os.path.abspath(
                    os.path.expandvars(os.path.expanduser(source))
                ))
            )
            if (
                not is_socket
                and self.path_policy.intersects_denied_tree(
                    normalized_source
                )
            ):
                raise PathAccessDeniedError(
                    "docker mount source is blocked by the filesystem policy"
                )
            docker_socket = docker_socket or is_socket
            normalized_mounts.append({
                "source": normalized_source,
                "target": target,
                "read_only": bool(mount.get("read_only", False)),
            })
            if not is_socket:
                for protected in (
                    self.tool_approval_policy
                    .hardline_paths_intersecting_mount(normalized_source)
                ):
                    hardline_targets.append(map_protected_target(
                        protected,
                        normalized_source,
                        target,
                    ))
                for protected in (
                    self.tool_approval_policy
                    .protected_paths_intersecting_mount(normalized_source)
                ):
                    configured_targets.append(map_protected_target(
                        protected,
                        normalized_source,
                        target,
                    ))
        self._mounts = tuple(normalized_mounts)
        self._docker_socket_exposed = docker_socket
        self._hardline_protected_targets = tuple(hardline_targets)
        self._configured_protected_targets = tuple(configured_targets)

    def _ensure_container(self):
        """启动一个长驻容器（只启动一次）。"""
        if self._container_id:
            return
        command = [
            "docker", "run", "-d",
            "--name", f"hermes-{self._session_id}",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", "256",
            "--cpus", "1",
            "--memory", "512m",
            "--tmpfs", "/tmp:rw,nosuid,size=256m",
        ]
        for mount in self._mounts:
            specification = (
                "type=bind,"
                f"source={mount['source']},"
                f"target={mount['target']}"
            )
            if mount["read_only"]:
                specification += ",readonly"
            command.extend(["--mount", specification])
        command.extend([self._image, "sleep", "infinity"])
        result = subprocess.run(
            command,
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

    def approval_risk_context(self) -> dict:
        """按实际挂载能力区分普通容器、宿主挂载和 Docker socket。"""
        if self._docker_socket_exposed:
            backend_type = "docker_socket"
        elif self._mounts:
            backend_type = "docker_host_mount"
        else:
            backend_type = "docker"
        return {
            "backend_type": backend_type,
            "host_mounts": bool(self._mounts),
            "docker_socket": self._docker_socket_exposed,
            "remote_host": False,
            # 该内部字段只参与 hardline 检查，不进入审批详情或普通日志。
            "hardline_protected_paths": self._hardline_protected_targets,
            "configured_protected_paths": self._configured_protected_targets,
        }

    def _update_cwd(self):
        """Docker：CWD 文件在容器内，通过 docker exec 读取。"""
        if not self._container_id:
            return
        result = subprocess.run(
            ["docker", "exec", self._container_id, "cat", self._cwd_shell],
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
