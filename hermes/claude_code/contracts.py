"""Claude Code 受管运行的稳定契约。"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
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
MAX_NATIVE_INTERACTION_PROMPT_CHARS = 8_192
MAX_NATIVE_INTERACTION_OPTIONS = 64


class ClaudeCodeState(str, Enum):
    """只描述 Claude Code 可观察语义，不替代 ProcessStatus。"""

    STARTING = "STARTING"
    READY = "READY"
    WORKING = "WORKING"
    WAITING_INPUT = "WAITING_INPUT"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    LOST = "LOST"
    UNKNOWN = "UNKNOWN"


class ClaudeCodeEventType(str, Enum):
    """一次观察可以产生的稳定事件类型。"""

    OUTPUT = "OUTPUT"
    PROGRESS = "PROGRESS"
    QUESTION = "QUESTION"
    APPROVAL_REQUEST = "APPROVAL_REQUEST"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    COMPLETION_SIGNAL = "COMPLETION_SIGNAL"
    FAILURE_SIGNAL = "FAILURE_SIGNAL"
    PROCESS_EXIT = "PROCESS_EXIT"
    READ_ERROR = "READ_ERROR"
    CURSOR_GAP = "CURSOR_GAP"
    UNKNOWN_PROMPT = "UNKNOWN_PROMPT"


class ClaudeCodeActionKind(str, Enum):
    """需要上层决定但 P5 不会自动执行的动作类别。"""

    CLARIFICATION = "clarification"
    APPROVAL = "approval"
    AUTHENTICATION = "authentication"
    DESTRUCTIVE_ACTION = "destructive_action"
    EXTERNAL_ACCESS = "external_access"
    UNKNOWN_PROMPT = "unknown_prompt"
    STALLED = "stalled"


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


def build_claude_code_action_id(
    *,
    process_id: str,
    session_owner: str,
    kind: ClaudeCodeActionKind,
    prompt_text: str,
    options: tuple[str, ...],
    cursor_start: int,
    cursor_end: int,
) -> str:
    """基于脱敏后的可观察提示生成不含正文的稳定动作身份。"""

    _require_nonempty_text("process_id", process_id)
    _require_nonempty_text("session_owner", session_owner)
    if not isinstance(kind, ClaudeCodeActionKind):
        raise ValueError("kind must be a ClaudeCodeActionKind")
    if not isinstance(prompt_text, str):
        raise ValueError("prompt_text must be text")
    if not isinstance(options, tuple) or any(
        not isinstance(option, str) or not option.strip()
        for option in options
    ):
        raise ValueError("options must contain non-empty text")
    _require_nonnegative_int("cursor_start", cursor_start)
    _require_nonnegative_int("cursor_end", cursor_end)
    if cursor_end < cursor_start:
        raise ValueError("cursor_end must not precede cursor_start")

    payload = (
        "claude-code-action-v1",
        process_id,
        session_owner,
        kind.value,
        cursor_start,
        cursor_end,
        " ".join(prompt_text.split()).casefold(),
        tuple(" ".join(option.split()).casefold() for option in options),
    )
    return "ccact_" + hashlib.sha256(
        repr(payload).encode("utf-8")
    ).hexdigest()


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


@dataclass(frozen=True, slots=True)
class ClaudeCodeEvent:
    """绑定 ProcessManager 原始 cursor 区间的脱敏增量事件。"""

    event_type: ClaudeCodeEventType
    process_id: str
    cursor_start: int
    cursor_end: int
    timestamp: float
    text: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.event_type, ClaudeCodeEventType):
            raise ValueError("event_type must be a ClaudeCodeEventType")
        _require_nonempty_text("process_id", self.process_id)
        _require_nonnegative_int("cursor_start", self.cursor_start)
        _require_nonnegative_int("cursor_end", self.cursor_end)
        if self.cursor_end < self.cursor_start:
            raise ValueError("cursor_end must not precede cursor_start")
        _require_timestamp("timestamp", self.timestamp)
        if not isinstance(self.text, str):
            raise ValueError("event text must be text")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("event metadata must be a mapping")
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(dict(self.metadata)),
        )


@dataclass(frozen=True, slots=True)
class ClaudeCodeActionRequired:
    """只报告待决策内容，不包含回答、批准或认证数据。"""

    kind: ClaudeCodeActionKind
    summary: str
    prompt_text: str
    options: tuple[str, ...]
    risk: str
    cursor: int
    action_id: str = ""
    process_id: str = ""
    session_owner: str = ""
    cursor_start: int | None = None
    cursor_end: int | None = None
    created_at: float | None = None
    raw_prompt_text: str | None = field(default=None, repr=False)
    raw_options: tuple[str, ...] | None = field(
        default=None,
        repr=False,
    )
    native_prompt_fingerprint: str | None = field(
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ClaudeCodeActionKind):
            raise ValueError("kind must be a ClaudeCodeActionKind")
        _require_nonempty_text("summary", self.summary)
        if not isinstance(self.prompt_text, str):
            raise ValueError("prompt_text must be text")
        if not isinstance(self.options, tuple) or any(
            not isinstance(option, str) or not option.strip()
            for option in self.options
        ):
            raise ValueError("options must contain non-empty text")
        if self.raw_prompt_text is not None:
            if not isinstance(self.raw_prompt_text, str):
                raise ValueError("raw_prompt_text must be text or None")
            if len(self.raw_prompt_text) > MAX_NATIVE_INTERACTION_PROMPT_CHARS:
                raise ValueError("raw_prompt_text exceeds the interaction limit")
        if self.raw_options is not None:
            if not isinstance(self.raw_options, tuple) or any(
                not isinstance(option, str) or not option.strip()
                for option in self.raw_options
            ):
                raise ValueError(
                    "raw_options must contain non-empty text or be None"
                )
            if len(self.raw_options) > MAX_NATIVE_INTERACTION_OPTIONS:
                raise ValueError("raw_options exceeds the interaction limit")
        if self.native_prompt_fingerprint is not None:
            if not isinstance(self.native_prompt_fingerprint, str):
                raise ValueError(
                    "native_prompt_fingerprint must be text or None"
                )
            if len(self.native_prompt_fingerprint) != 64 or any(
                character not in "0123456789abcdef"
                for character in self.native_prompt_fingerprint
            ):
                raise ValueError("native_prompt_fingerprint is invalid")
        _require_nonempty_text("risk", self.risk)
        _require_nonnegative_int("cursor", self.cursor)
        identity_present = any(
            (
                self.action_id,
                self.process_id,
                self.session_owner,
                self.cursor_start is not None,
                self.cursor_end is not None,
                self.created_at is not None,
            )
        )
        if not identity_present:
            return
        _require_nonempty_text("action_id", self.action_id)
        _require_nonempty_text("process_id", self.process_id)
        _require_nonempty_text("session_owner", self.session_owner)
        if self.cursor_start is None or self.cursor_end is None:
            raise ValueError("action cursor range must be complete")
        _require_nonnegative_int("cursor_start", self.cursor_start)
        _require_nonnegative_int("cursor_end", self.cursor_end)
        if self.cursor_end < self.cursor_start:
            raise ValueError("cursor_end must not precede cursor_start")
        if self.cursor != self.cursor_end:
            raise ValueError("cursor must equal cursor_end for an identified action")
        if self.created_at is None:
            raise ValueError("created_at is required for an identified action")
        _require_timestamp("created_at", self.created_at)


@dataclass(frozen=True, slots=True)
class ClaudeCodeCurrentInteraction:
    """只包含当前有效原生提示的透明展示视图。"""

    state: ClaudeCodeState
    action: ClaudeCodeActionRequired

    def __post_init__(self) -> None:
        if not isinstance(self.state, ClaudeCodeState):
            raise ValueError("state must be a ClaudeCodeState")
        if not isinstance(self.action, ClaudeCodeActionRequired):
            raise ValueError("action must be a ClaudeCodeActionRequired")
        _require_nonempty_text("action_id", self.action.action_id)
        _require_nonempty_text("process_id", self.action.process_id)
        _require_nonempty_text("session_owner", self.action.session_owner)
        if (
            self.action.raw_prompt_text is None
            or self.action.raw_options is None
        ):
            raise ValueError(
                "current interaction requires a native prompt view"
            )

    @property
    def action_id(self) -> str:
        return self.action.action_id

    @property
    def process_id(self) -> str:
        return self.action.process_id

    @property
    def session_owner(self) -> str:
        return self.action.session_owner

    @property
    def kind(self) -> ClaudeCodeActionKind:
        return self.action.kind

    @property
    def prompt_text(self) -> str:
        assert self.action.raw_prompt_text is not None
        return self.action.raw_prompt_text

    @property
    def options(self) -> tuple[str, ...]:
        assert self.action.raw_options is not None
        return self.action.raw_options

    @property
    def cursor_start(self) -> int:
        assert self.action.cursor_start is not None
        return self.action.cursor_start

    @property
    def cursor_end(self) -> int:
        assert self.action.cursor_end is not None
        return self.action.cursor_end

    @property
    def created_at(self) -> float:
        assert self.action.created_at is not None
        return self.action.created_at


@dataclass(frozen=True, slots=True)
class ClaudeCodeInteractionResponse:
    """仅在提交调用栈中短暂携带用户明确提供的原样回复。"""

    action_id: str
    process_id: str
    session_owner: str
    response: str = field(repr=False)
    user_confirmed: bool
    created_at: float

    def __post_init__(self) -> None:
        _require_nonempty_text("action_id", self.action_id)
        _require_nonempty_text("process_id", self.process_id)
        _require_nonempty_text("session_owner", self.session_owner)
        if not isinstance(self.response, str):
            raise ValueError("response must be text")
        if not isinstance(self.user_confirmed, bool):
            raise ValueError("user_confirmed must be a boolean")
        _require_timestamp("created_at", self.created_at)


@dataclass(frozen=True, slots=True)
class ClaudeCodeSnapshot:
    """单次观察的有界、脱敏且不触发交互的结构化结果。"""

    session_ref: ClaudeCodeSessionRef
    state: ClaudeCodeState
    events: tuple[ClaudeCodeEvent, ...]
    action_required: ClaudeCodeActionRequired | None
    raw_cursor: int
    normalized_output: str
    process_status: str | None
    exit_code: int | None
    last_activity_at: float
    last_observed_at: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.session_ref, ClaudeCodeSessionRef):
            raise ValueError("session_ref must be a ClaudeCodeSessionRef")
        if not isinstance(self.state, ClaudeCodeState):
            raise ValueError("state must be a ClaudeCodeState")
        if not isinstance(self.events, tuple) or any(
            not isinstance(event, ClaudeCodeEvent) for event in self.events
        ):
            raise ValueError("events must contain ClaudeCodeEvent values")
        if self.action_required is not None and not isinstance(
            self.action_required,
            ClaudeCodeActionRequired,
        ):
            raise ValueError(
                "action_required must be ClaudeCodeActionRequired or None"
            )
        _require_nonnegative_int("raw_cursor", self.raw_cursor)
        if not isinstance(self.normalized_output, str):
            raise ValueError("normalized_output must be text")
        if (
            self.process_status is not None
            and self.process_status not in CLAUDE_CODE_PROCESS_STATUSES
        ):
            raise ValueError("process_status is not supported")
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool)
            or not isinstance(self.exit_code, int)
        ):
            raise ValueError("exit_code must be an integer or None")
        _require_timestamp("last_activity_at", self.last_activity_at)
        if self.last_observed_at is not None:
            _require_timestamp("last_observed_at", self.last_observed_at)


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
    "MAX_NATIVE_INTERACTION_OPTIONS",
    "MAX_NATIVE_INTERACTION_PROMPT_CHARS",
    "ClaudeCodeActionKind",
    "ClaudeCodeActionRequired",
    "ClaudeCodeCurrentInteraction",
    "ClaudeCodeEvent",
    "ClaudeCodeEventType",
    "ClaudeCodeInteractionResponse",
    "ClaudeCodeProcessLog",
    "ClaudeCodeProcessPort",
    "ClaudeCodeProcessSnapshot",
    "ClaudeCodeReadResult",
    "ClaudeCodeRuntimeError",
    "ClaudeCodeSessionRef",
    "ClaudeCodeSnapshot",
    "ClaudeCodeState",
    "build_claude_code_action_id",
]
