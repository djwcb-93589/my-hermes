"""固定 Node 子项目的定位、检查与执行封装。"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .errors import DocxError


_MINIMUM_NODE_MAJOR = 20
_VERSION_PATTERN = re.compile(r"^v?(?P<major>\d+)(?:\.\d+){0,2}")


class NodeRuntime:
    """只允许执行模块内部固定创建脚本的 Node 运行时。"""

    def __init__(self, node_executable: str | Path | None = None) -> None:
        self._configured_executable = node_executable
        self._runtime_dir = Path(__file__).resolve().parent / "node_runtime"
        self._script_path = self._runtime_dir / "scripts" / "create.mjs"
        self._resolved_executable: Path | None = None
        self._node_version: str | None = None
        self._checked = False

    @property
    def node_version(self) -> str | None:
        """返回最近一次成功检查得到的 Node 版本。"""

        return self._node_version

    def check(self) -> None:
        """检查 Node 版本、固定脚本和本地 npm 依赖。"""

        self._checked = False
        self._resolved_executable = None
        self._node_version = None
        executable = self._resolve_executable()
        version = self._check_version(executable)
        required_files = (
            self._runtime_dir / "package.json",
            self._runtime_dir / "package-lock.json",
            self._script_path,
        )
        dependency_directory = self._runtime_dir / "node_modules" / "docx"
        if (
            not all(path.is_file() for path in required_files)
            or not dependency_directory.is_dir()
        ):
            raise DocxError(
                "node_dependencies_missing",
                "DOCX Node 依赖尚未准备，请先在安装阶段运行 npm ci。",
            )

        self._resolved_executable = executable
        self._node_version = version
        self._checked = True

    def run_create(
        self,
        spec_path: Path,
        output_path: Path,
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """调用固定脚本并解析唯一一行 JSON 结果。"""

        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise DocxError("invalid_request", "timeout_seconds 必须是大于零的有限数值。")
        if not spec_path.is_absolute() or not output_path.is_absolute():
            raise DocxError("invalid_request", "传给 Node runtime 的路径必须是绝对路径。")
        if not self._checked:
            self.check()

        executable = self._resolved_executable
        if executable is None:
            raise DocxError("node_runtime_unavailable", "Node 运行时不可用。")

        command = [
            str(executable),
            str(self._script_path),
            str(spec_path),
            str(output_path),
        ]
        try:
            completed = subprocess.run(
                command,
                shell=False,
                cwd=self._runtime_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=float(timeout_seconds),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DocxError(
                "node_execution_timeout",
                "DOCX 创建进程执行超时。",
            ) from exc
        except FileNotFoundError as exc:
            self._checked = False
            raise DocxError(
                "node_runtime_unavailable",
                "Node 运行时不可用。",
            ) from exc
        except OSError as exc:
            raise DocxError(
                "node_execution_failed",
                "无法启动 DOCX 创建进程。",
            ) from exc

        if completed.returncode != 0:
            error_type = self._read_child_error_type(completed.stdout, completed.stderr)
            if error_type == "invalid_request":
                raise DocxError("invalid_request", "Node runtime 拒绝了文档规格。")
            if error_type == "invalid_block":
                raise DocxError("invalid_block", "Node runtime 拒绝了文档内容块。")
            raise DocxError("node_execution_failed", "DOCX 创建进程执行失败。")

        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            raise DocxError("node_result_invalid", "Node runtime 返回了无效结果。")
        try:
            result = json.loads(lines[0])
        except json.JSONDecodeError as exc:
            raise DocxError("node_result_invalid", "Node runtime 返回了无效结果。") from exc
        if (
            not isinstance(result, dict)
            or result.get("ok") is not True
            or isinstance(result.get("block_count"), bool)
            or not isinstance(result.get("block_count"), int)
            or result["block_count"] < 0
        ):
            raise DocxError("node_result_invalid", "Node runtime 返回了无效结果。")
        return result

    def _resolve_executable(self) -> Path:
        configured = self._configured_executable
        if configured is None:
            configured = os.environ.get("MYHERMES_NODE_EXECUTABLE")

        if configured is None:
            discovered = shutil.which("node")
            if discovered is None:
                raise DocxError("node_runtime_unavailable", "未找到 Node 运行时。")
            return Path(discovered).resolve()

        raw_value = os.fspath(configured).strip()
        if not raw_value:
            raise DocxError("node_runtime_unavailable", "配置的 Node 路径为空。")

        candidate = Path(raw_value).expanduser()
        if candidate.is_absolute():
            executable = candidate.resolve()
        elif candidate.parent == Path("."):
            discovered = shutil.which(raw_value)
            if discovered is None:
                raise DocxError("node_runtime_unavailable", "配置的 Node 运行时不可用。")
            executable = Path(discovered).resolve()
        else:
            raise DocxError(
                "node_runtime_unavailable",
                "Node 路径必须是绝对路径或 PATH 中的命令名。",
            )

        if not executable.is_file():
            raise DocxError("node_runtime_unavailable", "配置的 Node 运行时不可用。")
        if os.name != "nt" and not os.access(executable, os.X_OK):
            raise DocxError("node_runtime_unavailable", "配置的 Node 运行时不可执行。")
        return executable

    def _check_version(self, executable: Path) -> str:
        try:
            completed = subprocess.run(
                [str(executable), "--version"],
                shell=False,
                cwd=self._runtime_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10.0,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DocxError("node_execution_timeout", "Node 版本检查超时。") from exc
        except (FileNotFoundError, OSError) as exc:
            raise DocxError("node_runtime_unavailable", "Node 运行时不可用。") from exc

        version = completed.stdout.strip()
        match = _VERSION_PATTERN.match(version)
        if completed.returncode != 0 or match is None:
            raise DocxError(
                "node_version_unsupported",
                "无法确认 Node 主版本，要求 Node 20 或更高版本。",
            )
        if int(match.group("major")) < _MINIMUM_NODE_MAJOR:
            raise DocxError(
                "node_version_unsupported",
                "Node 主版本过低，要求 Node 20 或更高版本。",
            )
        return version

    @staticmethod
    def _read_child_error_type(stdout: str, stderr: str) -> str | None:
        for stream in (stdout, stderr):
            lines = [line for line in stream.splitlines() if line.strip()]
            if len(lines) != 1:
                continue
            try:
                payload = json.loads(lines[0])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and isinstance(payload.get("error_type"), str):
                return payload["error_type"]
        return None
