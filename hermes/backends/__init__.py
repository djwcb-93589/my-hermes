"""
执行环境抽象。

BaseExecutionEnvironment 把每条命令包装成：CWD 追踪、环境变量快照（用于
同一 session 的命令间持久化）、超时处理。LocalBackend 另在创建子进程前
过滤基础设施凭证。具体后端只需实现 _run_bash() 和 cleanup()。

路径双重表示
------------
snapshot 和 cwd 临时文件需要两种路径形式：

  - ``_snapshot_shell`` / ``_cwd_shell``  —— 嵌入 bash 命令字符串里
    的形式（如 ``source /c/Users/.../snap.sh``）。
  - ``_snapshot_host``  / ``_cwd_host``   —— Python 用来 read/unlink
    同一个文件的形式（如 ``C:\\Users\\...\\snap.sh``）。

POSIX 上两者完全相同。Windows + Git Bash 上不同 —— LocalBackend 通过
覆盖 ``_setup_paths`` 来填充全部四个变量；``_cwd_to_shell`` 和
``_normalize_cwd`` 这两个 hook 负责 cwd 在两种形式间互转。
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from hermes.approval_security import (
    ApprovalSecurityPolicy,
    DEFAULT_APPROVAL_SECURITY_POLICY,
)
from hermes.config import (
    APPROVAL_SECURITY_POLICY,
    DB_PATH,
    PATH_ACCESS_POLICY,
    _config,
)
from hermes.path_policy import ALLOW_ALL_PATH_POLICY, PathAccessPolicy
from hermes.processes import BackgroundProcessHandle, BackgroundProcessOutput
from hermes.redaction import is_explicit_credential_env_name


# Local Terminal 的硬性基础设施凭证名单。这里维护项目实际使用或保留的
# 环境变量名，不按 KEY/TOKEN/SECRET 等普通子串做全量猜测。
INFRASTRUCTURE_CREDENTIAL_ENV_VARS = frozenset({
    "OPENAI_API_KEY",
    "FALLBACK_API_KEY",
    "ANTHROPIC_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "GITHUB_TOKEN",
    "FEISHU_APP_SECRET",
    "FEISHU_VERIFICATION_TOKEN",
    "FEISHU_ENCRYPT_KEY",
    "WEIXIN_TOKEN",
    # 为项目自身未来的内部服务保留一个明确名称，不扩展厂商名单。
    "HERMES_INTERNAL_SERVICE_TOKEN",
})

# 兼容旧的内部导入；新实现统一使用语义更明确的公开常量。
_SECRET_BLOCKLIST = INFRASTRUCTURE_CREDENTIAL_ENV_VARS

_GATEWAY_CREDENTIAL_FIELDS = frozenset({
    "app_secret",
    "verification_token",
    "encrypt_key",
    "token",
    "secret",
})
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _configured_infrastructure_credential_values(
    config: Mapping,
) -> frozenset[str]:
    """收集配置中真实生效的模型与 Gateway 凭证值。"""
    values: set[str] = set()

    def add(value) -> None:
        if isinstance(value, str) and value:
            values.add(value)

    add(config.get("api_key"))
    fallback_cfg = config.get("fallback", {})
    if isinstance(fallback_cfg, Mapping):
        add(fallback_cfg.get("api_key"))

    gateway_cfg = config.get("gateway", {})
    if isinstance(gateway_cfg, Mapping):
        platforms_cfg = gateway_cfg.get("platforms", {})
        if isinstance(platforms_cfg, Mapping):
            for platform_cfg in platforms_cfg.values():
                if not isinstance(platform_cfg, Mapping):
                    continue
                for field in _GATEWAY_CREDENTIAL_FIELDS:
                    add(platform_cfg.get(field))
    return frozenset(values)


def _load_terminal_env_passthrough(
    terminal_cfg: Mapping,
) -> frozenset[str]:
    """读取任务凭证显式透传名单；硬性基础设施名单不可覆盖。"""
    configured = terminal_cfg.get("env_passthrough", [])
    if configured is None:
        configured = []
    if not isinstance(configured, list) or not all(
        isinstance(name, str) for name in configured
    ):
        raise ValueError(
            "terminal.env_passthrough must be a list of environment "
            "variable names"
        )

    passthrough: set[str] = set()
    for raw_name in configured:
        name = raw_name.strip()
        if not _ENV_VAR_NAME_RE.fullmatch(name):
            raise ValueError(
                "terminal.env_passthrough contains an invalid environment "
                f"variable name: {raw_name!r}"
            )
        normalized = name.upper()
        if normalized in INFRASTRUCTURE_CREDENTIAL_ENV_VARS:
            raise ValueError(
                "terminal.env_passthrough cannot include protected "
                f"infrastructure credential: {name}"
            )
        passthrough.add(normalized)
    return frozenset(passthrough)


def filter_local_subprocess_environment(
    source_env: Mapping[str, str],
    *,
    env_passthrough: Iterable[str] = (),
    infrastructure_secret_values: Iterable[str] = (),
) -> dict[str, str]:
    """构造 Local 子进程环境，保留普通变量并隔离凭证。

    硬性基础设施变量及其已配置值永不继承。其它以明确凭证字段结尾的
    任务变量只有出现在 env_passthrough 中才继承；普通环境变量保持原样。
    """
    passthrough = frozenset(
        str(name).strip().upper()
        for name in env_passthrough
        if str(name).strip()
    )
    protected_values = frozenset(
        str(value)
        for value in infrastructure_secret_values
        if str(value)
    )

    filtered: dict[str, str] = {}
    for name, value in source_env.items():
        normalized = name.upper()
        if normalized in INFRASTRUCTURE_CREDENTIAL_ENV_VARS:
            continue
        if value and value in protected_values:
            continue
        if (
            is_explicit_credential_env_name(name)
            and normalized not in passthrough
        ):
            continue
        filtered[name] = value
    return filtered


class UnsupportedBackendError(Exception):
    """后端未实现该文件操作（如 Docker/SSH 默认不暴露文件 IO）。"""


class BackgroundProcessUnsupportedError(RuntimeError):
    """当前 Backend 暂不支持后台进程。"""


class BackgroundProcessCancelledError(RuntimeError):
    """后台进程在成功返回 Handle 前被取消。"""


class BackgroundProcessStartCleanupError(RuntimeError):
    """后台启动失败，清理无法确认，Handle 仍需由上层接管。"""

    def __init__(
        self,
        message: str,
        *,
        handle: BackgroundProcessHandle,
        start_error: BaseException | None = None,
        cleanup_error: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.handle = handle
        # 原始对象仅供内部诊断，公共消息始终保持稳定和脱敏。
        self._start_error = start_error
        self._cleanup_error = cleanup_error
        self.start_error_type = (
            None if start_error is None else type(start_error).__name__
        )
        self.cleanup_error_type = (
            None if cleanup_error is None else type(cleanup_error).__name__
        )


class BaseExecutionEnvironment(ABC):
    """
    所有 terminal 后端必须满足的契约。

    子类只需实现 _run_bash() 和 cleanup()。其它——命令包装、快照恢复、
    CWD 追踪、超时处理——都在基类里共享。
    """

    terminal_path_preflight_enabled = False
    backend_type = "unknown"

    def __init__(
        self,
        cwd: str,
        timeout: int = 180,
        *,
        path_policy: PathAccessPolicy | None = None,
        tool_approval_policy: ApprovalSecurityPolicy | None = None,
    ):
        self.cwd = cwd
        self.path_policy = path_policy or ALLOW_ALL_PATH_POLICY
        self.tool_approval_policy = (
            tool_approval_policy or DEFAULT_APPROVAL_SECURITY_POLICY
        )
        # 智能审批实现只能由可信 backend 创建上下文注入，默认永久关闭。
        self.intelligent_approval_advisor = None
        self.timeout = timeout
        self._session_id = uuid.uuid4().hex[:12]
        # 默认：/tmp/hermes-* （POSIX 下 shell == host）。Windows 上的
        # 子类通过覆盖 _setup_paths 把 shell / host 两种形式分开。
        stem = f"/tmp/hermes-{self._session_id}"
        self._snapshot_shell = f"{stem}-snap.sh"
        self._snapshot_host = self._snapshot_shell
        self._cwd_shell = f"{stem}-cwd.txt"
        self._cwd_host = self._cwd_shell
        self._snapshot_ready = False
        self._execute_lock = threading.Lock()
        # 子类 hook：重新定位临时文件。
        self._setup_paths()

    _CANCEL_POLL_INTERVAL = 0.1
    _INTERRUPT_GRACE_SECONDS = 2.0

    @abstractmethod
    def _run_bash(self, cmd_string: str, *, timeout: int) -> subprocess.Popen:
        """启动一个 bash 进程执行包装好的命令。"""
        ...

    @abstractmethod
    def cleanup(self):
        """释放后端专属资源。"""
        ...

    # --- 子类 hook ---

    def _setup_paths(self):
        """覆盖此方法以重新定位 snapshot/cwd 文件。需要同时设置四个变量：
        _snapshot_shell、_snapshot_host、_cwd_shell、_cwd_host。"""
        pass

    def _cwd_to_shell(self, cwd: str) -> str:
        """把 host 形式的 cwd 转成 bash 能识别的形式。"""
        return cwd

    def _normalize_cwd(self, raw: str) -> str:
        """把 `pwd -P` 的原始输出转回 host 形式。"""
        return raw

    def approval_risk_context(self) -> dict:
        """返回不含凭证、主机名和挂载源路径的 backend 风险画像。"""
        return {
            "backend_type": self.backend_type,
            "host_mounts": False,
            "docker_socket": False,
            "remote_host": self.backend_type == "ssh",
            "hardline_protected_paths": (),
            "configured_protected_paths": (),
        }

    def _interrupt_process(self, proc: subprocess.Popen) -> None:
        """请求当前命令尽快退出；LocalBackend 会覆盖为进程组中断。"""
        proc.terminate()

    def _kill_process_tree(self, proc: subprocess.Popen) -> None:
        """强制终止当前命令；默认只能保证终止直接子进程。"""
        proc.kill()

    def _release_process_resources(self, proc: subprocess.Popen) -> None:
        """释放子类附加到进程的资源。"""
        pass

    @staticmethod
    def _cancel_requested(cancel_checker: Callable[[], bool] | None) -> bool:
        """读取外部取消信号；检查器异常不能遗留失控子进程。"""
        if cancel_checker is None:
            return False
        try:
            return bool(cancel_checker())
        except Exception:
            return False

    def _stop_process(
        self,
        proc: subprocess.Popen,
        *,
        interrupt_first: bool,
    ) -> str:
        """先软中断、必要时强杀，并回收管道输出。"""
        if interrupt_first:
            try:
                self._interrupt_process(proc)
            except (OSError, ValueError):
                try:
                    self._kill_process_tree(proc)
                except (OSError, ValueError):
                    pass
        else:
            try:
                self._kill_process_tree(proc)
            except (OSError, ValueError):
                pass

        try:
            stdout, _ = proc.communicate(
                timeout=(self._INTERRUPT_GRACE_SECONDS if interrupt_first else 5),
            )
        except subprocess.TimeoutExpired:
            try:
                self._kill_process_tree(proc)
            except (OSError, ValueError):
                pass
            try:
                stdout, _ = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                # 极端情况下后代进程可能仍持有 stdout 管道；停止等待输出，
                # 防止 /stop 因等待 EOF 再次无界阻塞。
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                stdout = ""
        return stdout or ""

    # --- 共享逻辑 ---

    def init_session(self):
        """把当前 shell 环境捕获到 snapshot 文件里。"""
        init_cmd = (
            f"export -p > {self._snapshot_shell} 2>/dev/null; "
            f"pwd -P > {self._cwd_shell}"
        )
        proc = self._run_bash(init_cmd, timeout=10)
        try:
            proc.wait(timeout=10)
        finally:
            self._release_process_resources(proc)
        self._snapshot_ready = True

    def execute(
        self,
        command: str,
        timeout: int | None = None,
        *,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> dict:
        """串行执行单个 session 的命令，避免取消收尾与下一条命令竞态。"""
        with self._execute_lock:
            return self._execute(
                command,
                timeout=timeout,
                cancel_checker=cancel_checker,
            )

    def spawn_background(
        self,
        command: str,
        *,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> BackgroundProcessHandle:
        """启动后台进程；默认 Backend 显式声明尚不支持。"""

        raise BackgroundProcessUnsupportedError(
            "Current backend does not support background processes"
        )

    def _execute(
        self,
        command: str,
        timeout: int | None = None,
        *,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> dict:
        """包装 → 执行 → 等待 → 更新 CWD。返回 {"output": str, "returncode": int}。"""
        if self._cancel_requested(cancel_checker):
            return {
                "output": "(cancelled)",
                "returncode": 130,
                "cancelled": True,
            }
        if not self._snapshot_ready:
            self.init_session()

        if self._cancel_requested(cancel_checker):
            return {
                "output": "(cancelled)",
                "returncode": 130,
                "cancelled": True,
            }

        timeout = timeout or self.timeout
        wrapped = self._wrap_command(command)
        proc = self._run_bash(wrapped, timeout=timeout)
        deadline = time.monotonic() + timeout

        try:
            while True:
                if self._cancel_requested(cancel_checker):
                    self._stop_process(proc, interrupt_first=True)
                    return {
                        "output": "(cancelled)",
                        "returncode": 130,
                        "cancelled": True,
                    }

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._stop_process(proc, interrupt_first=False)
                    return {"output": "(timed out)", "returncode": 124}

                try:
                    stdout, _ = proc.communicate(
                        timeout=min(self._CANCEL_POLL_INTERVAL, remaining),
                    )
                    break
                except subprocess.TimeoutExpired:
                    continue
        except KeyboardInterrupt:
            # CLI 的 Ctrl+C 与 Agent /stop 走同一软中断语义。
            self._stop_process(proc, interrupt_first=True)
            raise
        except BaseException:
            self._stop_process(proc, interrupt_first=False)
            raise
        finally:
            self._release_process_resources(proc)

        self._update_cwd()

        output = stdout or ""
        return {"output": output[:10000], "returncode": proc.returncode or 0}

    def _wrap_command(self, command: str) -> str:
        """把裸命令包装成：恢复环境 → cd → 执行 → 保存环境 → 保存 CWD。"""
        import shlex
        parts = []
        if self._snapshot_ready:
            parts.append(f"source {self._snapshot_shell} 2>/dev/null")
        cwd_for_shell = self._cwd_to_shell(self.cwd)
        parts.append(f"cd {shlex.quote(cwd_for_shell)} 2>/dev/null")
        parts.append(command)
        parts.append(f"_exit=$?; export -p > {self._snapshot_shell} 2>/dev/null; "
                     f"pwd -P > {self._cwd_shell} 2>/dev/null; exit $_exit")
        return "; ".join(parts)

    def _update_cwd(self):
        """读取 CWD 文件，跟踪目录变化。"""
        try:
            new_cwd = Path(self._cwd_host).read_text(encoding="utf-8").strip()
            if new_cwd:
                self.cwd = self._normalize_cwd(new_cwd)
        except FileNotFoundError:
            pass

    # --- 文件 IO 抽象（terminal 工具之外的文件操作走这一组） ---

    def resolve_path(self, rel_path: str) -> str:
        """把（可能是相对的）路径解析成 host 形式的绝对路径。

        相对路径以 ``self.cwd`` 为基准。子类可覆盖以处理路径形式转换
        （如 Windows + Git Bash 把 MSYS 形式 ``/d/...`` 转成 Windows 形式）。
        """
        expanded = os.path.expandvars(os.path.expanduser(rel_path))
        p = Path(expanded)
        if not p.is_absolute():
            p = Path(self.cwd) / p
        return str(p)

    def read_file(self, path: str, offset: int = 0, limit: int | None = None) -> bytes:
        """读取文件二进制内容。默认不支持，子类按需覆盖。"""
        raise UnsupportedBackendError(
            f"{type(self).__name__} does not implement read_file"
        )

    def write_file(self, path: str, content: bytes, mode: str = "write") -> None:
        """写入文件。mode: "write" 覆盖，"append" 追加。默认不支持。"""
        raise UnsupportedBackendError(
            f"{type(self).__name__} does not implement write_file"
        )

    def list_dir(self, path: str) -> list[str]:
        """列目录，返回条目名列表。默认不支持。"""
        raise UnsupportedBackendError(
            f"{type(self).__name__} does not implement list_dir"
        )

    def stat_file(self, path: str) -> dict:
        """返回文件信息 {size, is_dir, is_file, mtime}。默认不支持。"""
        raise UnsupportedBackendError(
            f"{type(self).__name__} does not implement stat_file"
        )


def create_backend(
    config: dict,
    *,
    path_policy: PathAccessPolicy | None = None,
    tool_approval_policy: ApprovalSecurityPolicy | None = None,
) -> BaseExecutionEnvironment:
    """根据 config 选择合适的后端。"""
    # 局部 import，避免模块加载阶段产生循环引用。
    from hermes.backends.local import LocalBackend
    from hermes.backends.docker import DockerBackend
    from hermes.backends.ssh import SSHBackend

    terminal_cfg = config.get("terminal", {})
    if not isinstance(terminal_cfg, Mapping):
        raise ValueError("terminal config must be a mapping")
    backend_type = terminal_cfg.get("backend", "local")
    active_path_policy = path_policy or PATH_ACCESS_POLICY
    active_approval_policy = (
        tool_approval_policy or APPROVAL_SECURITY_POLICY
    )

    if backend_type == "docker":
        image = terminal_cfg.get("docker_image", "python:3.11-slim")
        return DockerBackend(
            image=image,
            cwd="/workspace",
            path_policy=active_path_policy,
            tool_approval_policy=active_approval_policy,
            mounts=terminal_cfg.get("docker_mounts", ()),
        )
    elif backend_type == "ssh":
        return SSHBackend(
            host=terminal_cfg["ssh_host"],
            user=terminal_cfg["ssh_user"],
            key_path=terminal_cfg.get("ssh_key"),
            cwd="~",
            path_policy=active_path_policy,
            tool_approval_policy=active_approval_policy,
        )
    else:
        return LocalBackend(
            cwd=os.getcwd(),
            path_policy=active_path_policy,
            tool_approval_policy=active_approval_policy,
            env_passthrough=_load_terminal_env_passthrough(terminal_cfg),
            infrastructure_secret_values=(
                _configured_infrastructure_credential_values(config)
            ),
        )


# ---------------------------------------------------------------------------
# 按 session 管理的 backend 注册表。
#
# 每个 conversation session 拿到自己独立的 backend 实例，cwd、环境快照
# 等状态不会跨 session 泄漏。session_key 由调用方用来标识一个会话：
# CLI 用 SQLite session 的 UUID；gateway 用平台维度的 session_key
# （例如 "agent:main:console:dm:console_user"）。
# ---------------------------------------------------------------------------

_backends: dict[str, BaseExecutionEnvironment] = {}
_backends_lock = threading.Lock()


def _clear_session_approval_state(session_key: str) -> None:
    """backend/session 清理时同步收敛 pending 和 session grant。"""
    from hermes.approval_policy import clear_session_grants
    from hermes.db import (
        cancel_pending_gateway_approvals_for_session,
        init_db,
    )

    clear_session_grants(session_key)
    try:
        conn = init_db(DB_PATH)
        try:
            cancel_pending_gateway_approvals_for_session(
                conn,
                session_key,
                decision_source="backend_cleanup",
            )
        finally:
            conn.close()
    except Exception as exc:
        print(
            "  [approval:audit] event=session_cleanup_failed "
            f"exception={type(exc).__name__}"
        )


def get_backend(session_key: str = "default") -> BaseExecutionEnvironment:
    """按 session_key 取或建 backend。

    同一个 session_key 的第一次调用会通过 create_backend(_config) 新建；
    后续调用直接返回缓存实例。线程安全。
    """
    with _backends_lock:
        b = _backends.get(session_key)
        if b is None:
            b = create_backend(_config, path_policy=PATH_ACCESS_POLICY)
            _backends[session_key] = b
        return b


def cleanup_backend(session_key: str) -> bool:
    """清理指定 session 的 backend。存在则返回 True。"""
    with _backends_lock:
        b = _backends.pop(session_key, None)
    _clear_session_approval_state(session_key)
    if b is None:
        return False
    try:
        b.cleanup()
    except Exception:
        pass
    return True


def cleanup_all_backends() -> None:
    """清理所有缓存的 backend。程序退出时调用。"""
    with _backends_lock:
        items = list(_backends.items())
        _backends.clear()
    for session_key, b in items:
        _clear_session_approval_state(session_key)
        try:
            b.cleanup()
        except Exception:
            pass


__all__ = [
    "INFRASTRUCTURE_CREDENTIAL_ENV_VARS",
    "BackgroundProcessHandle",
    "BackgroundProcessOutput",
    "BackgroundProcessCancelledError",
    "BackgroundProcessStartCleanupError",
    "BackgroundProcessUnsupportedError",
    "BaseExecutionEnvironment",
    "UnsupportedBackendError",
    "cleanup_all_backends",
    "cleanup_backend",
    "create_backend",
    "filter_local_subprocess_environment",
    "get_backend",
]
