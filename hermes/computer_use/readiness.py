"""cua-driver 安装与健康状态的独立诊断。"""

import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .transport import CuaDriverConfig, build_cua_driver_env


_SUPPORTED_PLATFORMS = frozenset({"win32", "darwin", "linux"})
_VERSION_TIMEOUT_SECONDS = 5.0
_DOCTOR_TIMEOUT_SECONDS = 12.0


def check_cua_driver_readiness(
    config: CuaDriverConfig,
) -> dict[str, Any]:
    """返回不启动 MCP 会话的 cua-driver 安装与健康诊断结果。"""

    platform = sys.platform
    result: dict[str, Any] = {
        "platform": platform,
        "platform_supported": platform in _SUPPORTED_PLATFORMS,
        "installed": False,
        "version": None,
        "ready": None,
        "checks": [],
        "error": None,
    }
    if not result["platform_supported"]:
        result["error"] = "unsupported_platform"
        return result

    if not _is_available_cwd(config.cwd):
        result["error"] = "invalid_cwd"
        return result

    environment = build_cua_driver_env(config.env)
    executable = config.command[0]
    version_process, version_error = _run_driver_command(
        [executable, "--version"],
        config=config,
        environment=environment,
        timeout=_VERSION_TIMEOUT_SECONDS,
    )
    if version_error is not None:
        result["installed"] = version_error != "driver_not_found"
        result["error"] = (
            "version_timeout"
            if version_error == "driver_timeout"
            else version_error
        )
        return result

    if version_process is None:
        result["error"] = "version_failed"
        return result

    result["installed"] = True
    if version_process.returncode == 0:
        result["version"] = _command_output(version_process)

    doctor_process, doctor_error = _run_driver_command(
        [executable, "doctor", "--json"],
        config=config,
        environment=environment,
        timeout=_DOCTOR_TIMEOUT_SECONDS,
    )
    if doctor_error is not None:
        result["error"] = (
            "doctor_timeout"
            if doctor_error == "driver_timeout"
            else doctor_error
        )
        return result
    if doctor_process is None or doctor_process.returncode != 0:
        result["error"] = "doctor_failed"
        return result

    try:
        doctor_result = json.loads(doctor_process.stdout)
    except (json.JSONDecodeError, TypeError, ValueError):
        result["error"] = "invalid_doctor_json"
        return result

    if not isinstance(doctor_result, Mapping):
        result["error"] = "invalid_doctor_result"
        return result
    ready = doctor_result.get("ok")
    checks = _normalize_checks(doctor_result.get("probes"))
    if type(ready) is not bool or checks is None:
        result["error"] = "invalid_doctor_result"
        return result

    result["ready"] = ready
    result["checks"] = checks
    return result


def _is_available_cwd(cwd: str | Path | None) -> bool:
    """确认可选工作目录能够被安全地传给子进程。"""

    if cwd is None:
        return True
    try:
        path = Path(cwd)
        return path.exists() and path.is_dir()
    except (OSError, TypeError, ValueError):
        return False


def _run_driver_command(
    command: list[str],
    *,
    config: CuaDriverConfig,
    environment: Mapping[str, str],
    timeout: float,
) -> tuple[subprocess.CompletedProcess[str] | None, str | None]:
    """执行有限时诊断命令，并将常见启动失败转换为稳定状态。"""

    try:
        process = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=config.cwd,
            env=dict(environment),
            shell=False,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return None, "driver_not_found"
    except subprocess.TimeoutExpired:
        return None, "driver_timeout"
    except (OSError, TypeError, ValueError):
        return None, "driver_start_failed"
    return process, None


def _command_output(process: subprocess.CompletedProcess[str]) -> str | None:
    """提取成功版本命令的非空文本，不暴露失败诊断输出。"""

    output = process.stdout.strip()
    return output or None


def _normalize_checks(probes: Any) -> list[dict[str, str]] | None:
    """将 doctor 探针缩减为公开的标签、状态和消息字段。"""

    if not isinstance(probes, list):
        return None

    checks: list[dict[str, str]] = []
    for probe in probes:
        if not isinstance(probe, Mapping):
            return None
        label = probe.get("label", "")
        status = probe.get("status", "")
        message = probe.get("message", "")
        if not all(
            isinstance(value, str)
            for value in (label, status, message)
        ):
            return None
        checks.append(
            {
                "label": label,
                "status": status,
                "message": message,
            }
        )
    return checks
