"""Claude Code 受管运行的稳定契约。"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol


CLAUDE_CODE_ACTIVE_PROCESS_STATUSES = frozenset({"starting", "running"})
CLAUDE_CODE_PROCESS_STATUSES = frozenset(
    {
        "starting",
        "running",
        "exited",
        "killed",
        "lost",
        "failed_start",
    }
)


class ClaudeCodeRuntimeError(RuntimeError):
    """携带稳定错误类型且不暴露底层异常正文。"""

    def __init__(
        self,
        error_type: str,
        safe_message: str,
        *,
        retryable: bool = False,
        delivery_unknown: bool = False,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(safe_message)
        self.error_type = error_type
        self.safe_message = safe_message
        self.retryable = bool(retryable)
        self.delivery_unknown = bool(delivery_unknown)
        self.details = MappingProxyType(dict(details or {}))

    def to_result(self) -> dict[str, object]:
        """导出不包含凭据、环境变量值或底层异常文本的结构化结果。"""

        result: dict[str, object] = {
            "ok": False,
            "error_type": self.error_type,
            "error": self.safe_message,
            "retryable": self.retryable,
        }
        if self.delivery_unknown:
            result["delivery_unknown"] = True
        result.update(self.details)
        return result


def _require_nonempty_text(field_name: str, value: str) -> None:
    """校验必须跨运行边界保存的非空文本。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_nonnegative_int(field_name: str, value: int) -> None:
    """拒绝 bool 并校验非负整数。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


def _require_timestamp(field_name: str, value: float) -> None:
    """校验有限的非负时间戳。"""

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{field_name} must be a non-negative timestamp")


@dataclass(frozen=True, slots=True)
class ClaudeCodeSessionRef:
    """只保存受管 Claude Code 会话所需的最小运行引用。"""

    process_id: str
    session_owner: str
    cwd: str
    cursor: int
    started_at: float
    last_activity_at: float

    def __post_init__(self) -> None:
        _require_nonempty_text("process_id", self.process_id)
        _require_nonempty_text("session_owner", self.session_owner)
        _require_nonempty_text("cwd", self.cwd)
        _require_nonnegative_int("cursor", self.cursor)
        _require_timestamp("started_at", self.started_at)
        _require_timestamp("last_activity_at", self.last_activity_at)


@dataclass(frozen=True, slots=True)
class ClaudeCodeProcessSnapshot:
    """与 ProcessManager 内部记录解耦的只读生命周期快照。"""

    process_id: str
    status: str
    cwd: str
    terminal_mode: str
    exit_code: int | None
    started_at: float
    finished_at: float | None
    output_base_cursor: int
    output_end_cursor: int

    def __post_init__(self) -> None:
        _require_nonempty_text("process_id", self.process_id)
        if self.status not in CLAUDE_CODE_PROCESS_STATUSES:
            raise ValueError("status is not a supported ProcessStatus")
        _require_nonempty_text("cwd", self.cwd)
        if self.terminal_mode not in {"pipe", "pty"}:
            raise ValueError("terminal_mode must be pipe or pty")
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool)
            or not isinstance(self.exit_code, int)
        ):
            raise ValueError("exit_code must be an integer or None")
        _require_timestamp("started_at", self.started_at)
        if self.finished_at is not None:
            _require_timestamp("finished_at", self.finished_at)
        _require_nonnegative_int("output_base_cursor", self.output_base_cursor)
        _require_nonnegative_int("output_end_cursor", self.output_end_cursor)

    @property
    def active(self) -> bool:
        """只按 ProcessStatus 判断进程是否仍占用 cwd。"""

        return self.status in CLAUDE_CODE_ACTIVE_PROCESS_STATUSES


@dataclass(frozen=True, slots=True)
class ClaudeCodeProcessLog:
    """保留 ProcessManager 绝对 cursor 的一次增量读取。"""

    process_id: str
    status: str
    output: str
    requested_cursor: int
    available_from_cursor: int
    next_cursor: int
    output_truncated: bool
    exit_code: int | None

    def __post_init__(self) -> None:
        _require_nonempty_text("process_id", self.process_id)
        if self.status not in CLAUDE_CODE_PROCESS_STATUSES:
            raise ValueError("status is not a supported ProcessStatus")
        if not isinstance(self.output, str):
            raise ValueError("output must be text")
        _require_nonnegative_int("requested_cursor", self.requested_cursor)
        _require_nonnegative_int(
            "available_from_cursor",
            self.available_from_cursor,
        )
        _require_nonnegative_int("next_cursor", self.next_cursor)
        if not isinstance(self.output_truncated, bool):
            raise ValueError("output_truncated must be a boolean")
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool)
            or not isinstance(self.exit_code, int)
        ):
            raise ValueError("exit_code must be an integer or None")


@dataclass(frozen=True, slots=True)
class ClaudeCodeReadResult:
    """返回原始 PTY 文本和更新后的最小会话引用。"""

    session: ClaudeCodeSessionRef
    status: str
    output: str
    requested_cursor: int
    available_from_cursor: int
    next_cursor: int
    output_truncated: bool
    exit_code: int | None


class ClaudeCodeProcessPort(Protocol):
    """Claude Code runtime 使用的最小进程端口。"""

    def preflight_start(
        self,
        *,
        session_owner: str,
        cwd: str,
        executable: str,
    ) -> str:
        """验证启动能力并返回 PathPolicy 规范化后的 cwd。"""

    def start(
        self,
        *,
        session_owner: str,
        cwd: str,
        executable: str,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> ClaudeCodeProcessSnapshot:
        """通过唯一 ProcessManager 启动并登记后台 PTY。"""

    def read(
        self,
        *,
        session_owner: str,
        process_id: str,
        cursor: int,
        limit: int,
    ) -> ClaudeCodeProcessLog:
        """按 ProcessManager 的绝对 cursor 读取新增输出。"""

    def write(
        self,
        *,
        session_owner: str,
        process_id: str,
        data: str,
    ) -> int:
        """原样写入文本，不提交 Enter。"""

    def submit(
        self,
        *,
        session_owner: str,
        process_id: str,
        data: str,
    ) -> int:
        """写入文本并按 PTY transport 提交 Enter。"""

    def status(
        self,
        *,
        session_owner: str,
        process_id: str,
    ) -> ClaudeCodeProcessSnapshot:
        """返回 ProcessManager 的生命周期快照。"""

    def wait(
        self,
        *,
        session_owner: str,
        process_id: str,
        timeout: float,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> ClaudeCodeProcessSnapshot:
        """执行一次有界等待。"""

    def interrupt(
        self,
        *,
        session_owner: str,
        process_id: str,
    ) -> int:
        """通过受 owner 保护的输入路径发送 Ctrl+C。"""

    def kill(
        self,
        *,
        session_owner: str,
        process_id: str,
        grace_seconds: float,
    ) -> ClaudeCodeProcessSnapshot:
        """把强制终止与进程树清理交给 ProcessManager。"""


__all__ = [
    "CLAUDE_CODE_ACTIVE_PROCESS_STATUSES",
    "CLAUDE_CODE_PROCESS_STATUSES",
    "ClaudeCodeProcessLog",
    "ClaudeCodeProcessPort",
    "ClaudeCodeProcessSnapshot",
    "ClaudeCodeReadResult",
    "ClaudeCodeRuntimeError",
    "ClaudeCodeSessionRef",
]
