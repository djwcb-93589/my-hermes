"""cua-driver 进程生命周期和 MCP stdio 通信。"""

import atexit
import json
import math
import subprocess
import threading
import weakref
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import (
    ActionTimeoutError,
    BackendDisconnectedError,
    BackendStartError,
    BackendUnavailableError,
    ComputerUseError,
    DriverNotFoundError,
    ProtocolError,
)
from .environment import build_cua_driver_env


_MCP_PROTOCOL_VERSION = "2025-11-25"
_MAX_STDERR_LINES = 100
_MAX_STDERR_LINE_CHARS = 2_048
_MAX_EXPIRED_REQUEST_IDS = 256
_ACTIVE_CLIENTS: weakref.WeakSet[Any] = weakref.WeakSet()
_ACTIVE_CLIENTS_LOCK = threading.Lock()


def _register_active_client(client: Any) -> None:
    """登记已完成初始化的客户端，供进程退出时尽力清理。"""

    with _ACTIVE_CLIENTS_LOCK:
        _ACTIVE_CLIENTS.add(client)


def _unregister_active_client(client: Any) -> None:
    """移除已停止或启动失败的客户端登记。"""

    with _ACTIVE_CLIENTS_LOCK:
        _ACTIVE_CLIENTS.discard(client)


def _stop_active_clients_at_exit() -> None:
    """在解释器退出时逐个尽力关闭仍然活动的驱动客户端。"""

    try:
        with _ACTIVE_CLIENTS_LOCK:
            clients = tuple(_ACTIVE_CLIENTS)
    except BaseException:
        return
    for client in clients:
        try:
            client.stop()
        except BaseException:
            continue


atexit.register(_stop_active_clients_at_exit)


@dataclass(slots=True)
class CuaDriverConfig:
    """启动 cua-driver 和控制通信超时所需的独立配置。"""

    command: Sequence[str]
    startup_timeout: float = 10.0
    request_timeout: float = 30.0
    shutdown_timeout: float = 5.0
    cwd: str | Path | None = None
    env: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        """复制可变配置并拒绝无效命令或无限超时。"""

        if isinstance(self.command, (str, bytes)):
            raise ValueError("command must be a sequence of arguments")
        copied_command = list(self.command)
        if not copied_command:
            raise ValueError("command must not be empty")
        self.command = copied_command
        self.env = dict(self.env) if self.env is not None else None

        for name in (
            "startup_timeout",
            "request_timeout",
            "shutdown_timeout",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative value")
            setattr(self, name, value)


@dataclass(slots=True)
class _PendingRequest:
    """等待 stdout 读取线程交付单个 JSON-RPC 响应。"""

    event: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: ComputerUseError | None = None


class CuaDriverClient:
    """管理一个 cua-driver 子进程及其 MCP stdio 会话。"""

    def __init__(self, config: CuaDriverConfig) -> None:
        """复制配置并创建尚未启动的客户端。"""

        self._config = CuaDriverConfig(
            command=config.command,
            startup_timeout=config.startup_timeout,
            request_timeout=config.request_timeout,
            shutdown_timeout=config.shutdown_timeout,
            cwd=config.cwd,
            env=config.env,
        )
        self._process: subprocess.Popen[str] | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._started = False

        self._lifecycle_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._stderr_lock = threading.Lock()

        self._next_request_id = 1
        self._pending: dict[int, _PendingRequest] = {}
        self._expired_request_ids: deque[int] = deque(
            maxlen=_MAX_EXPIRED_REQUEST_IDS
        )
        self._transport_error: ComputerUseError | None = None
        self._stderr_lines: deque[str] = deque(maxlen=_MAX_STDERR_LINES)

    def start(self) -> None:
        """启动 cua-driver 并完成 MCP 初始化握手。"""

        with self._lifecycle_lock:
            if self.is_alive():
                return
            if self._process is not None:
                self._stop_locked()

            self._prepare_start()
            try:
                self._validate_cwd()
                process = subprocess.Popen(
                    list(self._config.command),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    cwd=self._config.cwd,
                    env=build_cua_driver_env(self._config.env),
                    shell=False,
                )
                self._process = process
                self._start_reader_threads(process)

                initialize_result = self._request_internal(
                    "initialize",
                    {
                        "protocolVersion": _MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {
                            "name": "hermes-computer-use",
                            "version": "0.1.0",
                        },
                    },
                    timeout=self._config.startup_timeout,
                    require_started=False,
                )
                self._validate_initialize_result(initialize_result)
                self._notify_internal(
                    "notifications/initialized",
                    params=None,
                    require_started=False,
                )
            except FileNotFoundError as exc:
                self._stop_locked()
                raise DriverNotFoundError(
                    "cua-driver command was not found.",
                    details={"exception_type": type(exc).__name__},
                ) from exc
            except ComputerUseError:
                self._stop_locked()
                raise
            except Exception as exc:
                self._stop_locked()
                raise BackendStartError(
                    "Failed to start cua-driver.",
                    details={"exception_type": type(exc).__name__},
                ) from exc
            else:
                self._started = True
                _register_active_client(self)

    def stop(self) -> None:
        """安全关闭 cua-driver；重复调用不会产生副作用。"""

        with self._lifecycle_lock:
            self._stop_locked()

    def is_alive(self) -> bool:
        """返回完成初始化的 cua-driver 是否仍在运行。"""

        with self._lifecycle_lock:
            process = self._process
            return (
                self._started
                and process is not None
                and process.poll() is None
            )

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """发送 JSON-RPC 请求并在配置的期限内等待结果。"""

        return self._request_internal(
            method,
            params,
            timeout=self._config.request_timeout,
            require_started=True,
        )

    def notify(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
    ) -> None:
        """发送不需要响应的 JSON-RPC 通知。"""

        self._notify_internal(
            method,
            params,
            require_started=True,
        )

    def list_tools(self) -> list[dict[str, Any]]:
        """返回 cua-driver 声明的 MCP 工具描述。"""

        result = self.request("tools/list")
        tools = result.get("tools")
        if not isinstance(tools, list) or any(
            not isinstance(tool, dict) for tool in tools
        ):
            raise ProtocolError(
                "cua-driver returned an invalid tools/list result.",
                details={"reason": "invalid_tools"},
            )
        return [dict(tool) for tool in tools]

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """调用 MCP 工具并返回未经 Computer Use 转换的结果。"""

        return self.request(
            "tools/call",
            {
                "name": name,
                "arguments": (
                    dict(arguments) if arguments is not None else {}
                ),
            },
        )

    def _prepare_start(self) -> None:
        """为新的子进程会话清空有限的运行时状态。"""

        self._started = False
        self._stop_event = threading.Event()
        with self._pending_lock:
            self._pending.clear()
            self._expired_request_ids.clear()
            self._transport_error = None
        with self._stderr_lock:
            self._stderr_lines.clear()

    def _validate_cwd(self) -> None:
        """在启动子进程前确认工作目录存在且为目录。"""

        cwd = self._config.cwd
        if cwd is None:
            return
        try:
            path = Path(cwd)
            is_available = path.exists() and path.is_dir()
        except (OSError, ValueError):
            is_available = False
        if not is_available:
            raise BackendStartError(
                "cua-driver working directory is unavailable.",
                details={"reason": "invalid_cwd"},
            )

    def _start_reader_threads(
        self,
        process: subprocess.Popen[str],
    ) -> None:
        """为 stdout 和 stderr 启动独立守护读取线程。"""

        stop_event = self._stop_event
        if stop_event is None:
            raise BackendStartError("cua-driver stop event is unavailable.")
        if process.stdout is None or process.stderr is None:
            raise BackendStartError("cua-driver stdio pipes are unavailable.")

        self._stdout_thread = threading.Thread(
            target=self._read_stdout,
            args=(process, stop_event),
            name="cua-driver-stdout",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr,
            args=(process, stop_event),
            name="cua-driver-stderr",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _request_internal(
        self,
        method: str,
        params: Mapping[str, Any] | None,
        *,
        timeout: float,
        require_started: bool,
    ) -> dict[str, Any]:
        """注册请求、发送消息并等待 stdout 线程交付结果。"""

        process = self._require_process(require_started=require_started)
        pending = _PendingRequest()
        with self._pending_lock:
            if self._transport_error is not None:
                raise self._transport_error
            request_id = self._next_request_id
            self._next_request_id += 1
            self._pending[request_id] = pending

        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            message["params"] = dict(params)

        try:
            self._send_message(message, process)
        except Exception:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise

        if not pending.event.wait(timeout):
            with self._pending_lock:
                active = self._pending.pop(request_id, None)
                if active is not None:
                    self._expired_request_ids.append(request_id)
                transport_error = self._transport_error

            if pending.event.is_set():
                return self._resolve_pending(pending)
            if transport_error is not None:
                raise transport_error
            returncode = process.poll()
            if returncode is not None:
                raise BackendDisconnectedError(
                    "cua-driver exited before responding.",
                    details={"returncode": returncode},
                )
            raise ActionTimeoutError(
                "Timed out waiting for cua-driver response.",
                details={"method": method, "timeout": timeout},
            )

        return self._resolve_pending(pending)

    def _notify_internal(
        self,
        method: str,
        params: Mapping[str, Any] | None,
        *,
        require_started: bool,
    ) -> None:
        """发送内部或公开 JSON-RPC 通知。"""

        process = self._require_process(require_started=require_started)
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            message["params"] = dict(params)
        self._send_message(message, process)

    def _require_process(
        self,
        *,
        require_started: bool,
    ) -> subprocess.Popen[str]:
        """返回可通信进程，否则抛出统一生命周期异常。"""

        process = self._process
        if require_started and not self._started:
            raise BackendUnavailableError(
                "cua-driver client is not started."
            )
        if process is None:
            raise BackendDisconnectedError(
                "cua-driver process is not available."
            )
        returncode = process.poll()
        if returncode is not None:
            raise BackendDisconnectedError(
                "cua-driver process has exited.",
                details={"returncode": returncode},
            )
        with self._pending_lock:
            transport_error = self._transport_error
        if transport_error is not None:
            raise transport_error
        return process

    def _send_message(
        self,
        message: Mapping[str, Any],
        process: subprocess.Popen[str],
    ) -> None:
        """在写锁保护下发送单行 UTF-8 JSON-RPC 消息。"""

        try:
            encoded = json.dumps(
                dict(message),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ProtocolError(
                "Unable to encode JSON-RPC message.",
                details={"exception_type": type(exc).__name__},
            ) from exc

        try:
            with self._write_lock:
                if self._process is not process or process.poll() is not None:
                    raise BrokenPipeError
                stdin = process.stdin
                if stdin is None or stdin.closed:
                    raise BrokenPipeError
                stdin.write(encoded)
                stdin.write("\n")
                stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            error = BackendDisconnectedError(
                "Failed to write to cua-driver.",
                details={"exception_type": type(exc).__name__},
            )
            self._set_transport_error(error, process)
            raise error from exc

    def _read_stdout(
        self,
        process: subprocess.Popen[str],
        stop_event: threading.Event,
    ) -> None:
        """持续读取 stdout 并按响应 ID 分发。"""

        stdout = process.stdout
        if stdout is None:
            return
        try:
            while not stop_event.is_set():
                line = stdout.readline()
                if line == "":
                    break
                self._handle_stdout_line(line, process)
        except (OSError, UnicodeError, ValueError) as exc:
            if not stop_event.is_set():
                self._set_transport_error(
                    BackendDisconnectedError(
                        "Failed to read from cua-driver.",
                        details={"exception_type": type(exc).__name__},
                    ),
                    process,
                )
        finally:
            if not stop_event.is_set():
                returncode = process.poll()
                details = (
                    {"returncode": returncode}
                    if returncode is not None
                    else None
                )
                self._set_transport_error(
                    BackendDisconnectedError(
                        "cua-driver stdout closed unexpectedly.",
                        details=details,
                    ),
                    process,
                )

    def _read_stderr(
        self,
        process: subprocess.Popen[str],
        stop_event: threading.Event,
    ) -> None:
        """持续清空 stderr，并仅保留有限长度的最近内容。"""

        stderr = process.stderr
        if stderr is None:
            return
        try:
            while not stop_event.is_set():
                line = stderr.readline()
                if line == "":
                    break
                if stop_event.is_set():
                    break
                recent = line.rstrip("\r\n")[-_MAX_STDERR_LINE_CHARS:]
                with self._stderr_lock:
                    self._stderr_lines.append(recent)
        except (OSError, UnicodeError, ValueError):
            return

    def _handle_stdout_line(
        self,
        line: str,
        process: subprocess.Popen[str],
    ) -> None:
        """验证单个 JSON-RPC 响应并交给对应请求。"""

        if self._process is not process:
            return
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            self._set_transport_error(
                ProtocolError(
                    "cua-driver returned invalid JSON.",
                    details={"reason": "invalid_json"},
                ),
                process,
            )
            return

        if not isinstance(payload, dict):
            self._set_transport_error(
                ProtocolError(
                    "cua-driver returned a non-object response.",
                    details={"reason": "non_object_response"},
                ),
                process,
            )
            return
        if payload.get("jsonrpc") != "2.0":
            self._set_transport_error(
                ProtocolError(
                    "cua-driver returned an invalid JSON-RPC version.",
                    details={"reason": "invalid_jsonrpc_version"},
                ),
                process,
            )
            return

        if "method" in payload:
            self._handle_server_message(payload, process)
            return

        if "id" not in payload:
            self._set_transport_error(
                ProtocolError(
                    "cua-driver response is missing a request ID.",
                    details={"reason": "missing_response_id"},
                ),
                process,
            )
            return
        if type(payload["id"]) is not int:
            self._set_transport_error(
                ProtocolError(
                    "cua-driver response has an invalid request ID.",
                    details={"reason": "invalid_response_id"},
                ),
                process,
            )
            return

        request_id = payload["id"]
        has_result = "result" in payload
        has_error = "error" in payload
        pending_error: ComputerUseError | None = None
        result: dict[str, Any] | None = None

        if has_result == has_error:
            pending_error = ProtocolError(
                "cua-driver returned an invalid response structure.",
                details={"reason": "invalid_response_shape"},
            )
        elif has_error:
            error_payload = payload["error"]
            if not isinstance(error_payload, dict):
                pending_error = ProtocolError(
                    "cua-driver returned an invalid JSON-RPC error.",
                    details={"reason": "invalid_error_object"},
                )
            else:
                error_code = error_payload.get("code")
                details = (
                    {"jsonrpc_code": error_code}
                    if type(error_code) in (int, str)
                    else {"reason": "jsonrpc_error"}
                )
                pending_error = ProtocolError(
                    "cua-driver returned a JSON-RPC error.",
                    details=details,
                )
        else:
            result_payload = payload["result"]
            if not isinstance(result_payload, dict):
                pending_error = ProtocolError(
                    "cua-driver returned a non-object result.",
                    details={"reason": "invalid_result"},
                )
            else:
                result = dict(result_payload)

        with self._pending_lock:
            pending = self._pending.pop(request_id, None)
            if pending is None:
                if request_id in self._expired_request_ids:
                    self._expired_request_ids.remove(request_id)
                    return
            else:
                pending.result = result
                pending.error = pending_error
                pending.event.set()
                return

        self._set_transport_error(
            ProtocolError(
                "cua-driver returned an unexpected response ID.",
                details={"reason": "unexpected_response_id"},
            ),
            process,
        )

    def _handle_server_message(
        self,
        payload: Mapping[str, Any],
        process: subprocess.Popen[str],
    ) -> None:
        """忽略服务端通知并响应服务端请求。"""

        method = payload["method"]
        if not isinstance(method, str):
            self._set_transport_error(
                ProtocolError(
                    "cua-driver message has an invalid method.",
                    details={"reason": "invalid_method"},
                ),
                process,
            )
            return

        if "id" not in payload:
            return

        request_id = payload["id"]
        if not (
            isinstance(request_id, str)
            or type(request_id) is int
        ):
            self._set_transport_error(
                ProtocolError(
                    "cua-driver request has an invalid request ID.",
                    details={"reason": "invalid_server_request_id"},
                ),
                process,
            )
            return

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

        try:
            self._send_message(response, process)
        except ComputerUseError:
            return

    def _resolve_pending(
        self,
        pending: _PendingRequest,
    ) -> dict[str, Any]:
        """返回请求结果或抛出读取线程交付的异常。"""

        if pending.error is not None:
            raise pending.error
        if pending.result is None:
            raise ProtocolError(
                "cua-driver response did not contain a result.",
                details={"reason": "missing_result"},
            )
        return pending.result

    def _set_transport_error(
        self,
        error: ComputerUseError,
        process: subprocess.Popen[str],
    ) -> None:
        """记录首个传输错误并唤醒所有等待请求。"""

        if self._process is not process:
            return
        with self._pending_lock:
            if self._transport_error is None:
                self._transport_error = error
            pending_requests = list(self._pending.values())
            self._pending.clear()
        for pending in pending_requests:
            pending.error = error
            pending.event.set()

    def _validate_initialize_result(
        self,
        result: Mapping[str, Any],
    ) -> None:
        """确认 MCP initialize 至少返回有效协议版本。"""

        protocol_version = result.get("protocolVersion")
        if protocol_version != _MCP_PROTOCOL_VERSION:
            raise ProtocolError(
                "cua-driver returned an invalid initialize result.",
                details={"reason": "unsupported_protocol_version"},
            )

    def _stop_locked(self) -> None:
        """在生命周期锁内关闭进程并清空所有运行时状态。"""

        try:
            self._started = False
            process = self._process
            stop_event = self._stop_event
            if stop_event is not None:
                stop_event.set()

            self._fail_pending_requests(
                BackendDisconnectedError("cua-driver client has stopped.")
            )

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

            self._join_reader(self._stdout_thread)
            self._join_reader(self._stderr_thread)
        finally:
            try:
                self._clear_runtime_state()
            finally:
                _unregister_active_client(self)

    def _fail_pending_requests(self, error: ComputerUseError) -> None:
        """使所有等待请求立即收到统一异常。"""

        with self._pending_lock:
            pending_requests = list(self._pending.values())
            self._pending.clear()
        for pending in pending_requests:
            pending.error = error
            pending.event.set()

    def _close_stdin(self, process: subprocess.Popen[str]) -> None:
        """在写锁内关闭子进程 stdin。"""

        with self._write_lock:
            stdin = process.stdin
            if stdin is None or stdin.closed:
                return
            try:
                stdin.close()
            except (OSError, ValueError):
                return

    @staticmethod
    def _wait_for_exit(
        process: subprocess.Popen[str],
        timeout: float,
    ) -> bool:
        """在有限时间内等待进程退出。"""

        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        return True

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        """请求终止仍在运行的子进程。"""

        if process.poll() is not None:
            return
        try:
            process.terminate()
        except OSError:
            return

    @staticmethod
    def _kill_process(process: subprocess.Popen[str]) -> None:
        """强制结束仍未退出的子进程。"""

        if process.poll() is not None:
            return
        try:
            process.kill()
        except OSError:
            return

    @staticmethod
    def _close_output_streams(process: subprocess.Popen[str]) -> None:
        """关闭 stdout 和 stderr 以解除读取线程阻塞。"""

        for stream in (process.stdout, process.stderr):
            if stream is None or stream.closed:
                continue
            try:
                stream.close()
            except (OSError, ValueError):
                continue

    def _join_reader(self, thread: threading.Thread | None) -> None:
        """在有限时间内等待读取线程结束。"""

        if thread is None or thread is threading.current_thread():
            return
        thread.join(timeout=max(self._config.shutdown_timeout, 1.0))

    def _clear_runtime_state(self) -> None:
        """清空进程、线程、请求和有限缓存。"""

        self._process = None
        self._stdout_thread = None
        self._stderr_thread = None
        self._stop_event = None
        self._started = False
        with self._pending_lock:
            self._pending.clear()
            self._expired_request_ids.clear()
            self._transport_error = None
        with self._stderr_lock:
            self._stderr_lines.clear()
