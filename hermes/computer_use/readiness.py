"""cua-driver 安装与健康状态的独立诊断。"""

import json
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .transport import CuaDriverConfig, build_cua_driver_env


_SUPPORTED_PLATFORMS = frozenset({"win32", "darwin", "linux"})
_MCP_PROTOCOL_VERSION = "2025-11-25"
_MAX_DIAGNOSTIC_TEXT_LENGTH = 512


@dataclass(slots=True)
class _VersionProbe:
    """保存版本命令的安装状态、文本和退出结果。"""

    installed: bool
    text: str | None
    succeeded: bool
    error: str | None = None


class _ReadinessMcpError(Exception):
    """临时 MCP 诊断会话的内部异常基类。"""


class _DriverNotFound(_ReadinessMcpError):
    """诊断命令找不到 cua-driver 可执行文件。"""


class _McpStartFailed(_ReadinessMcpError):
    """临时 MCP 进程无法启动或维持通信。"""


class _McpTimeout(_ReadinessMcpError):
    """临时 MCP 请求在限定时间内未完成。"""


class _McpProtocolError(_ReadinessMcpError):
    """临时 MCP 会话收到了不兼容的协议数据。"""


class _McpToolUnavailable(_ReadinessMcpError):
    """MCP 工具不存在、被拒绝或返回 JSON-RPC 错误。"""


class _TemporaryMcpSession:
    """只供 readiness 使用且不登记为正式客户端的短生命周期 MCP 会话。"""

    def __init__(
        self,
        config: CuaDriverConfig,
        environment: Mapping[str, str],
    ) -> None:
        """保存诊断配置并初始化尚未启动的通信状态。"""

        self._config = config
        self._environment = dict(environment)
        self._process: subprocess.Popen[str] | None = None
        self._stdout_lines: queue.Queue[str | None] = queue.Queue()
        self._stop_event = threading.Event()
        self._write_lock = threading.Lock()
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._next_request_id = 1

    def start(self) -> None:
        """启动临时 cua-driver mcp 进程并完成初始化握手。"""

        executable = self._config.command[0]
        try:
            process = subprocess.Popen(
                [executable, "mcp"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=self._config.cwd,
                env=self._environment,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise _DriverNotFound from exc
        except (OSError, TypeError, ValueError) as exc:
            raise _McpStartFailed from exc

        self._process = process
        try:
            self._start_reader_threads(process)
            result = self._request(
                "initialize",
                {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "hermes-computer-use-readiness",
                        "version": "0.1.0",
                    },
                },
                timeout=self._config.startup_timeout,
            )
            if result.get("protocolVersion") != _MCP_PROTOCOL_VERSION:
                raise _McpProtocolError
            self._notify("notifications/initialized")
        except _ReadinessMcpError:
            raise
        except Exception as exc:
            raise _McpStartFailed from exc

    def call_tool(self, name: str) -> dict[str, Any]:
        """调用一个无参数的诊断工具并保留原始 MCP 工具结果。"""

        return self._request(
            "tools/call",
            {
                "name": name,
                "arguments": {},
            },
            timeout=self._config.request_timeout,
            allow_tool_error=True,
        )

    def close(self) -> None:
        """无论诊断结果如何都关闭临时进程及其读取线程。"""

        self._stop_event.set()
        process = self._process
        try:
            if process is not None:
                self._close_stdin(process)
                if not self._wait_for_exit(
                    process,
                    self._config.shutdown_timeout,
                ):
                    self._terminate_process(process)
                    if not self._wait_for_exit(
                        process,
                        self._config.shutdown_timeout,
                    ):
                        self._kill_process(process)
                        self._wait_for_exit(
                            process,
                            max(self._config.shutdown_timeout, 1.0),
                        )
                self._close_output_streams(process)
        finally:
            self._join_reader(self._stdout_thread)
            self._join_reader(self._stderr_thread)
            self._process = None
            self._stdout_thread = None
            self._stderr_thread = None

    def _start_reader_threads(
        self,
        process: subprocess.Popen[str],
    ) -> None:
        """持续读取 stdout 和 stderr，避免诊断进程被管道缓冲区阻塞。"""

        if process.stdout is None or process.stderr is None:
            raise _McpStartFailed
        self._stdout_thread = threading.Thread(
            target=self._read_stdout,
            args=(process,),
            name="cua-driver-readiness-stdout",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(process,),
            name="cua-driver-readiness-stderr",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _read_stdout(self, process: subprocess.Popen[str]) -> None:
        """将 stdout 的每行 JSON-RPC 数据交给同步请求循环。"""

        stdout = process.stdout
        if stdout is None:
            self._stdout_lines.put(None)
            return
        try:
            while not self._stop_event.is_set():
                line = stdout.readline()
                if line == "":
                    break
                self._stdout_lines.put(line)
        except (OSError, UnicodeError, ValueError):
            pass
        finally:
            self._stdout_lines.put(None)

    def _drain_stderr(self, process: subprocess.Popen[str]) -> None:
        """持续清空 stderr，不保存可能包含敏感信息的输出。"""

        stderr = process.stderr
        if stderr is None:
            return
        try:
            while not self._stop_event.is_set():
                line = stderr.readline()
                if line == "":
                    break
        except (OSError, UnicodeError, ValueError):
            return

    def _request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout: float,
        allow_tool_error: bool = False,
    ) -> dict[str, Any]:
        """发送一个请求并在期限内读取与其 ID 匹配的响应。"""

        request_id = self._next_request_id
        self._next_request_id += 1
        self._send_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": dict(params),
            }
        )
        return self._wait_for_response(
            request_id,
            timeout=timeout,
            allow_tool_error=allow_tool_error,
        )

    def _notify(self, method: str) -> None:
        """发送初始化完成通知，不等待服务端响应。"""

        self._send_message(
            {
                "jsonrpc": "2.0",
                "method": method,
            }
        )

    def _wait_for_response(
        self,
        request_id: int,
        *,
        timeout: float,
        allow_tool_error: bool,
    ) -> dict[str, Any]:
        """忽略通知、响应服务端请求，并解析当前请求的响应。"""

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _McpTimeout
            try:
                line = self._stdout_lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise _McpTimeout from exc
            if line is None:
                raise _McpStartFailed

            payload = self._parse_message(line)
            if "method" in payload:
                self._handle_server_message(payload)
                continue

            if "id" not in payload or type(payload["id"]) is not int:
                raise _McpProtocolError
            if payload["id"] != request_id:
                raise _McpProtocolError

            has_result = "result" in payload
            has_error = "error" in payload
            if has_result == has_error:
                raise _McpProtocolError
            if has_error:
                if not isinstance(payload["error"], Mapping):
                    raise _McpProtocolError
                if allow_tool_error:
                    raise _McpToolUnavailable
                raise _McpProtocolError

            result = payload["result"]
            if not isinstance(result, Mapping):
                raise _McpProtocolError
            return dict(result)

    def _parse_message(self, line: str) -> dict[str, Any]:
        """解析并验证一行 JSON-RPC 对象。"""

        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise _McpProtocolError from exc
        if not isinstance(payload, Mapping):
            raise _McpProtocolError
        if payload.get("jsonrpc") != "2.0":
            raise _McpProtocolError
        return dict(payload)

    def _handle_server_message(self, payload: Mapping[str, Any]) -> None:
        """忽略服务端通知，并以标准 JSON-RPC 响应服务端请求。"""

        method = payload["method"]
        if not isinstance(method, str):
            raise _McpProtocolError
        if "id" not in payload:
            return

        request_id = payload["id"]
        if not (isinstance(request_id, str) or type(request_id) is int):
            raise _McpProtocolError
        if method == "ping":
            response: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {},
            }
        else:
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": "Method not found",
                },
            }
        self._send_message(response)

    def _send_message(self, message: Mapping[str, Any]) -> None:
        """在写锁保护下发送单行 UTF-8 JSON-RPC 消息。"""

        process = self._process
        if process is None or process.poll() is not None:
            raise _McpStartFailed
        try:
            encoded = json.dumps(
                dict(message),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise _McpProtocolError from exc

        try:
            with self._write_lock:
                stdin = process.stdin
                if stdin is None or stdin.closed:
                    raise BrokenPipeError
                stdin.write(encoded)
                stdin.write("\n")
                stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise _McpStartFailed from exc

    @staticmethod
    def _wait_for_exit(
        process: subprocess.Popen[str],
        timeout: float,
    ) -> bool:
        """在有限时间内等待诊断进程退出。"""

        try:
            process.wait(timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return True

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        """请求终止仍在运行的临时诊断进程。"""

        if process.poll() is not None:
            return
        try:
            process.terminate()
        except OSError:
            return

    @staticmethod
    def _kill_process(process: subprocess.Popen[str]) -> None:
        """强制结束仍未退出的临时诊断进程。"""

        if process.poll() is not None:
            return
        try:
            process.kill()
        except OSError:
            return

    def _close_stdin(self, process: subprocess.Popen[str]) -> None:
        """关闭诊断进程 stdin，促使其正常退出。"""

        with self._write_lock:
            stdin = process.stdin
            if stdin is None or stdin.closed:
                return
            try:
                stdin.close()
            except (OSError, ValueError):
                return

    @staticmethod
    def _close_output_streams(process: subprocess.Popen[str]) -> None:
        """关闭输出流以解除读取线程的阻塞。"""

        for stream in (process.stdout, process.stderr):
            if stream is None or stream.closed:
                continue
            try:
                stream.close()
            except (OSError, ValueError):
                continue

    def _join_reader(self, thread: threading.Thread | None) -> None:
        """在有限时间内等待一个诊断读取线程结束。"""

        if thread is None or thread is threading.current_thread():
            return
        thread.join(timeout=max(self._config.shutdown_timeout, 1.0))


def check_cua_driver_readiness(
    config: CuaDriverConfig,
) -> dict[str, Any]:
    """返回不启动正式 Backend 的 cua-driver 安装与健康诊断结果。"""

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

    session: _TemporaryMcpSession | None = None
    try:
        environment = build_cua_driver_env(config.env)
        version_probe = _read_version(config, environment)
        result["installed"] = version_probe.installed
        result["version"] = version_probe.text
        if version_probe.error is not None:
            result["error"] = version_probe.error
            return result

        session = _TemporaryMcpSession(config, environment)
        session.start()
        health_result = _call_tool_if_available(session, "health_report")
        health_report = _parse_health_report(health_result)
        if health_report is not None:
            result["ready"] = health_report["ready"]
            result["checks"] = health_report["checks"]
            return result

        fallback = _run_fallback_probes(
            session,
            platform=platform,
            version_succeeded=version_probe.succeeded,
        )
        result["ready"] = fallback["ready"]
        result["checks"] = fallback["checks"]
        return result
    except _DriverNotFound:
        result["installed"] = False
        result["error"] = "driver_not_found"
        return result
    except _McpTimeout:
        result["error"] = "mcp_timeout"
        return result
    except _McpProtocolError:
        result["error"] = "mcp_protocol_error"
        return result
    except _McpStartFailed:
        result["error"] = "mcp_start_failed"
        return result
    except Exception:
        result["error"] = "health_check_failed"
        return result
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


def _read_version(
    config: CuaDriverConfig,
    environment: Mapping[str, str],
) -> _VersionProbe:
    """读取版本首行，并把非零退出视为可继续诊断的已安装状态。"""

    executable = config.command[0]
    try:
        process = subprocess.run(
            [executable, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=config.cwd,
            env=dict(environment),
            shell=False,
            timeout=5.0,
            check=False,
        )
    except FileNotFoundError:
        return _VersionProbe(
            installed=False,
            text=None,
            succeeded=False,
            error="driver_not_found",
        )
    except subprocess.TimeoutExpired:
        return _VersionProbe(
            installed=True,
            text=None,
            succeeded=False,
            error="version_timeout",
        )
    except (OSError, TypeError, ValueError):
        return _VersionProbe(
            installed=True,
            text=None,
            succeeded=False,
            error="health_check_failed",
        )

    return _VersionProbe(
        installed=True,
        text=_first_output_line(process.stdout, process.stderr),
        succeeded=process.returncode == 0,
    )


def _first_output_line(stdout: str, stderr: str) -> str | None:
    """按 stdout、stderr 顺序返回第一条非空且长度受限的文本。"""

    for output in (stdout, stderr):
        for line in output.splitlines():
            value = line.strip()
            if value:
                return value[:_MAX_DIAGNOSTIC_TEXT_LENGTH]
    return None


def _call_tool_if_available(
    session: _TemporaryMcpSession,
    name: str,
) -> dict[str, Any] | None:
    """将工具不存在或拒绝转换为可继续的 fallback 信号。"""

    try:
        return session.call_tool(name)
    except _McpToolUnavailable:
        return None


def _parse_health_report(
    raw_result: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """优先解析有效结构化报告，再兼容 JSON 文本报告。"""

    if raw_result is None or raw_result.get("isError") is True:
        return None

    report = _parse_health_report_payload(
        raw_result.get("structuredContent"),
    )
    if report is not None:
        return report

    for payload in _json_content_payloads(raw_result):
        report = _parse_health_report_payload(payload)
        if report is not None:
            return report
    return None


def _parse_health_report_payload(
    payload: Any,
) -> dict[str, Any] | None:
    """验证单个候选载荷是否为 health_report 的正式结构。"""

    if not isinstance(payload, Mapping):
        return None
    if payload.get("schema_version") != "1":
        return None
    overall = payload.get("overall")
    checks = payload.get("checks")
    if overall not in {"ok", "degraded", "failed"}:
        return None
    if not isinstance(checks, list):
        return None
    return {
        "ready": overall == "ok",
        "checks": _normalize_checks(checks),
    }


def _run_fallback_probes(
    session: _TemporaryMcpSession,
    *,
    platform: str,
    version_succeeded: bool,
) -> dict[str, Any]:
    """在同一临时 MCP 会话中尽力收集权限和应用列表诊断。"""

    checks: list[dict[str, str]] = []
    permission_values: dict[str, bool] = {}
    permission_result = _call_tool_if_available(
        session,
        "check_permissions",
    )
    permission_checks, permission_values = _permission_checks(permission_result)
    checks.extend(permission_checks)

    list_apps_result = _call_tool_if_available(session, "list_apps")
    list_apps_succeeded = _list_apps_call_succeeded(list_apps_result)
    checks.append(
        {
            "label": "list_apps",
            "status": "pass" if list_apps_succeeded else "fail",
            "message": "",
        }
    )

    return {
        "ready": _fallback_ready(
            platform=platform,
            version_succeeded=version_succeeded,
            list_apps_succeeded=list_apps_succeeded,
            permission_values=permission_values,
        ),
        "checks": checks,
    }


def _extract_tool_payload(
    raw_result: Mapping[str, Any] | None,
) -> Any | None:
    """优先读取 structuredContent，其次解析 JSON 文本 content。"""

    if raw_result is None or raw_result.get("isError") is True:
        return None
    structured_content = raw_result.get("structuredContent")
    if structured_content is not None:
        return structured_content

    content = raw_result.get("content")
    if not isinstance(content, list):
        return None
    for payload in _json_content_payloads(raw_result):
        return payload
    return None


def _json_content_payloads(
    raw_result: Mapping[str, Any],
) -> list[Any]:
    """解析 content 中所有有效的 JSON 文本候选。"""

    content = raw_result.get("content")
    if not isinstance(content, list):
        return []

    payloads: list[Any] = []
    for item in content:
        if not isinstance(item, Mapping) or item.get("type") != "text":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        try:
            payloads.append(json.loads(text))
        except json.JSONDecodeError:
            continue
    return payloads


def _normalize_checks(checks: list[Any]) -> list[dict[str, str]]:
    """保留检查的正式字段，并安全转换非字符串标量。"""

    normalized: list[dict[str, str]] = []
    for check in checks:
        if isinstance(check, Mapping):
            label = _safe_text(check.get("label", ""))
            status = _safe_text(check.get("status", ""))
            message = _safe_text(check.get("message", ""))
        else:
            label = ""
            status = ""
            message = ""
        normalized.append(
            {
                "label": label,
                "status": status,
                "message": message,
            }
        )
    return normalized


def _safe_text(value: Any) -> str:
    """将公开诊断文本限制为安全的短字符串。"""

    if isinstance(value, str):
        return value[:_MAX_DIAGNOSTIC_TEXT_LENGTH]
    if isinstance(value, (bool, int, float)):
        return str(value)
    return ""


def _permission_checks(
    raw_result: Mapping[str, Any] | None,
) -> tuple[list[dict[str, str]], dict[str, bool]]:
    """将 check_permissions 中的布尔字段转换为正式检查结果。"""

    payload = _extract_tool_payload(raw_result)
    if not isinstance(payload, Mapping):
        return [], {}

    values: dict[str, bool] = {}
    _collect_boolean_fields(payload, prefix="", values=values)
    checks = [
        {
            "label": label,
            "status": "pass" if value else "fail",
            "message": "",
        }
        for label, value in values.items()
    ]
    return checks, values


def _collect_boolean_fields(
    value: Mapping[str, Any],
    *,
    prefix: str,
    values: dict[str, bool],
) -> None:
    """递归收集映射中的布尔权限字段，不保留复杂原始对象。"""

    for key, item in value.items():
        name = _safe_text(key)
        if not name:
            continue
        label = f"{prefix}.{name}" if prefix else name
        if type(item) is bool:
            values[label] = item
        elif isinstance(item, Mapping):
            _collect_boolean_fields(item, prefix=label, values=values)


def _list_apps_call_succeeded(
    raw_result: Mapping[str, Any] | None,
) -> bool:
    """仅依据 list_apps 调用是否成功判断驱动调用链状态。"""

    return raw_result is not None and raw_result.get("isError") is not True


def _fallback_ready(
    *,
    platform: str,
    version_succeeded: bool,
    list_apps_succeeded: bool,
    permission_values: Mapping[str, bool],
) -> bool | None:
    """按平台组合版本、应用列表和 macOS 权限的可验证状态。"""

    accessibility = _find_permission_value(
        permission_values,
        "accessibility",
    )
    screen_recording = _find_permission_value(
        permission_values,
        "screen_recording",
    )
    if platform == "darwin":
        if accessibility is False or screen_recording is False:
            return False
        if not version_succeeded:
            return None
        if not list_apps_succeeded:
            return False
        if accessibility is True and screen_recording is True:
            return True
        return None

    if not version_succeeded:
        return None
    return list_apps_succeeded


def _find_permission_value(
    values: Mapping[str, bool],
    permission: str,
) -> bool | None:
    """从不同命名风格的布尔权限字段中找出指定 macOS 权限。"""

    for label, value in values.items():
        normalized_label = "".join(
            character.lower()
            for character in label
            if character.isalnum()
        )
        if permission == "accessibility" and "accessibility" in normalized_label:
            return value
        if permission == "screen_recording" and (
            "screenrecording" in normalized_label
            or (
                "screen" in normalized_label
                and "record" in normalized_label
            )
        ):
            return value
    return None


def _is_available_cwd(cwd: str | Path | None) -> bool:
    """确认可选工作目录能够被安全地传给子进程。"""

    if cwd is None:
        return True
    try:
        path = Path(cwd)
        return path.exists() and path.is_dir()
    except (OSError, TypeError, ValueError):
        return False
