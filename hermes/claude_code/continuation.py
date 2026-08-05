"""Claude Code ActionRequired 的短生命周期、进程内续接状态。

该模块只保存安全的 action 投影和路由身份。原生终端视图以及用户回复
只在一次调用栈内使用，绝不进入此模块或会话持久化。
"""

from __future__ import annotations

import threading
import time
import math
from dataclasses import dataclass
from typing import Callable, Mapping


MAX_CONTINUATION_ENTRIES = 256
DEFAULT_CONTINUATION_TTL_SECONDS = 900.0
MAX_SAFE_SUMMARY = 512
MAX_SAFE_PROMPT = 4096
MAX_SAFE_OPTIONS = 16
MAX_SAFE_OPTION_LENGTH = 512
MAX_ID_LENGTH = 1024

# 仅允许明确的身份失效错误清除 continuation。未知错误按临时错误处理，
# 这样一次内部异常不会让用户失去仍然可能有效的原生交互。
_IDENTITY_STALE_ERROR_TYPES = frozenset(
    {
        "owner_mismatch",
        "controller_owner_mismatch",
        "session_owner_mismatch",
        "process_not_found",
        "session_not_found",
        "controller_task_not_found",
        "task_not_found",
        "controller_round_not_found",
        "round_not_found",
        "round_mismatch",
        "terminal_session",
        "interaction_stale",
        "interaction_not_found",
        "interaction_absent",
        "interaction_expired",
        "interaction_already_resolved",
        "interaction_id_mismatch",
        "action_required_absent",
        "interaction_consumed",
        "terminal_no_interaction",
    }
)
_TRANSIENT_ERROR_TYPES = frozenset(
    {
        "poll_failed",
        "runtime_unavailable",
        "controller_unavailable",
        "read_failed",
        "status_failed",
        "wait_failed",
        "interaction_response_failed",
        "interaction_response_unavailable",
        "interaction_in_progress",
        "deadline_exceeded",
        "timeout",
        "cancelled",
        "startup_not_ready",
        "coordinator_error",
        "coordinator_unavailable",
        "cleanup_failed",
    }
)


def classify_continuation_error(
    error_type: object,
    *,
    retryable: bool = False,
) -> str:
    """按稳定错误类型决定保留还是清除 continuation。"""

    normalized = error_type.strip().lower() if isinstance(error_type, str) else ""
    if normalized in _IDENTITY_STALE_ERROR_TYPES:
        return "stale"
    if normalized in _TRANSIENT_ERROR_TYPES or retryable:
        return "transient"
    # 未知的协调层错误不能证明 action 已失效，保守保留。
    return "transient"


def is_identity_stale_error(error_type: object) -> bool:
    return classify_continuation_error(error_type) == "stale"

def _safe_text(value: object, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        return "" if allow_empty else "unknown"
    normalized = " ".join(value.split())
    if not normalized and not allow_empty:
        return "unknown"
    return normalized[:maximum]


def _safe_identity(value: object, field: str) -> str:
    result = _safe_text(value, MAX_ID_LENGTH)
    if result == "unknown":
        raise ValueError(f"{field} is required")
    return result


@dataclass(frozen=True, slots=True)
class ClaudeCodePendingInteraction:
    """可跨一条真实用户消息保存的最小安全身份和 action 摘要。"""

    owner: str
    environment: str
    originating_conversation_id: str
    process_id: str
    round_id: str | None
    action_id: str
    kind: str
    safe_summary: str
    safe_prompt: str
    safe_options: tuple[str, ...]
    created_at: float
    expires_at: float
    cwd: str | None = None
    delivery_unknown: bool = False
    source_message_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "owner",
            "environment",
            "originating_conversation_id",
            "process_id",
            "action_id",
            "kind",
        ):
            object.__setattr__(self, name, _safe_identity(getattr(self, name), name))
        object.__setattr__(self, "safe_summary", _safe_text(self.safe_summary, MAX_SAFE_SUMMARY))
        object.__setattr__(self, "safe_prompt", _safe_text(self.safe_prompt, MAX_SAFE_PROMPT, allow_empty=True))
        options = tuple(
            _safe_text(option, MAX_SAFE_OPTION_LENGTH)
            for option in tuple(self.safe_options or ())[:MAX_SAFE_OPTIONS]
            if _safe_text(option, MAX_SAFE_OPTION_LENGTH)
        )
        object.__setattr__(self, "safe_options", options)
        if self.round_id is not None:
            object.__setattr__(self, "round_id", _safe_identity(self.round_id, "round_id"))
        if self.cwd is not None:
            object.__setattr__(self, "cwd", _safe_text(self.cwd, MAX_ID_LENGTH, allow_empty=True) or None)
        if self.source_message_id is not None:
            object.__setattr__(self, "source_message_id", _safe_identity(self.source_message_id, "source_message_id"))
        if (
            isinstance(self.created_at, bool)
            or isinstance(self.expires_at, bool)
            or not isinstance(self.created_at, (int, float))
            or not isinstance(self.expires_at, (int, float))
            or not math.isfinite(float(self.created_at))
            or not math.isfinite(float(self.expires_at))
            or self.created_at < 0
        ):
            raise ValueError("continuation timestamps must be numeric")
        if self.expires_at <= self.created_at:
            raise ValueError("continuation expiry must be after creation")
        if not isinstance(self.delivery_unknown, bool):
            raise ValueError("delivery_unknown must be boolean")

    @property
    def identity(self) -> tuple[str, str, str | None, str]:
        return (self.process_id, self.action_id, self.round_id, self.owner)

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at

    def to_safe_dict(self) -> dict:
        """返回可以交给 CLI/Gateway 展示的安全投影。"""

        return {
            "owner": self.owner,
            "environment": self.environment,
            "conversation_id": self.originating_conversation_id,
            "process_id": self.process_id,
            "round_id": self.round_id,
            "action_id": self.action_id,
            "kind": self.kind,
            "summary": self.safe_summary,
            "prompt_text": self.safe_prompt,
            "options": list(self.safe_options),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "cwd": self.cwd,
            "delivery_unknown": self.delivery_unknown,
            "source_message_id": self.source_message_id,
        }

    @classmethod
    def from_safe_dict(cls, value: Mapping[str, object]) -> "ClaudeCodePendingInteraction":
        if not isinstance(value, Mapping):
            raise ValueError("pending interaction must be a mapping")
        options = value.get("options", ())
        if isinstance(options, str) or not isinstance(options, (list, tuple)):
            options = ()
        return cls(
            owner=value.get("owner", ""),
            environment=value.get("environment", ""),
            originating_conversation_id=value.get("conversation_id", ""),
            process_id=value.get("process_id", ""),
            round_id=value.get("round_id"),
            action_id=value.get("action_id", ""),
            kind=value.get("kind", "unknown_prompt"),
            safe_summary=value.get("summary", "Claude Code requires input"),
            safe_prompt=value.get("prompt_text", ""),
            safe_options=tuple(options),
            created_at=float(value.get("created_at", 0.0)),
            expires_at=float(value.get("expires_at", 0.0)),
            cwd=value.get("cwd"),
            delivery_unknown=bool(value.get("delivery_unknown", False)),
            source_message_id=value.get("source_message_id"),
        )


class ClaudeCodeContinuationConflict(RuntimeError):
    """同一 owner 已经存在不能被静默覆盖的 action。"""


class ClaudeCodeContinuationStore:
    """每个 CLI/Gateway 实例独有的有界临时 pending 存储。"""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        ttl_seconds: float = DEFAULT_CONTINUATION_TTL_SECONDS,
        max_entries: int = MAX_CONTINUATION_ENTRIES,
    ) -> None:
        if not callable(clock) or ttl_seconds <= 0 or max_entries <= 0:
            raise ValueError("invalid continuation store configuration")
        self._clock = clock
        self._ttl_seconds = min(float(ttl_seconds), 3600.0)
        self._max_entries = min(int(max_entries), MAX_CONTINUATION_ENTRIES)
        self._entries: dict[str, ClaudeCodePendingInteraction] = {}
        self._lock = threading.RLock()

    def _purge_locked(self, now: float) -> None:
        expired = [owner for owner, item in self._entries.items() if item.is_expired(now)]
        for owner in expired:
            self._entries.pop(owner, None)

    def get(self, owner: str) -> ClaudeCodePendingInteraction | None:
        with self._lock:
            now = self._clock()
            self._purge_locked(now)
            return self._entries.get(owner)

    def upsert(
        self,
        pending: ClaudeCodePendingInteraction,
        *,
        replace_existing: bool = False,
    ) -> str:
        with self._lock:
            now = self._clock()
            self._purge_locked(now)
            existing = self._entries.get(pending.owner)
            if existing is not None and existing.identity != pending.identity:
                if not replace_existing:
                    raise ClaudeCodeContinuationConflict(
                        "a different Claude Code interaction is already pending"
                    )
                if existing.process_id != pending.process_id:
                    raise ClaudeCodeContinuationConflict(
                        "pending interaction belongs to another process"
                    )
            if existing is None and len(self._entries) >= self._max_entries:
                oldest = min(self._entries, key=lambda key: self._entries[key].created_at)
                self._entries.pop(oldest, None)
            if existing is not None and existing.identity == pending.identity and existing.delivery_unknown:
                values = {
                    field: getattr(pending, field)
                    for field in pending.__dataclass_fields__
                }
                values["delivery_unknown"] = True
                pending = ClaudeCodePendingInteraction(**values)
            self._entries[pending.owner] = pending
            return "same" if existing is not None and existing.identity == pending.identity else "replaced" if existing is not None else "created"

    def clear_if_identity(
        self,
        owner: str,
        *,
        process_id: str,
        action_id: str,
        round_id: str | None = None,
    ) -> bool:
        with self._lock:
            current = self._entries.get(owner)
            if current is None:
                return False
            if current.process_id != process_id or current.action_id != action_id:
                return False
            if round_id is not None and current.round_id != round_id:
                return False
            self._entries.pop(owner, None)
            return True

    def clear(self, owner: str) -> None:
        with self._lock:
            self._entries.pop(owner, None)

    def mark_delivery_unknown(self, owner: str, *, process_id: str, action_id: str) -> bool:
        with self._lock:
            current = self._entries.get(owner)
            if current is None or current.process_id != process_id or current.action_id != action_id:
                return False
            values = {
                field: getattr(current, field)
                for field in current.__dataclass_fields__
            }
            values["delivery_unknown"] = True
            self._entries[owner] = ClaudeCodePendingInteraction(**values)
            return True

    def apply_observation(
        self,
        observation: Mapping[str, object],
    ) -> tuple[str, ClaudeCodePendingInteraction | None]:
        """兼容旧调用点，统一使用 observation reconciliation。"""

        return self.reconcile_observation(observation)

    def reconcile_observation(
        self,
        observation: Mapping[str, object],
    ) -> tuple[str, ClaudeCodePendingInteraction | None]:
        """按 Controller 的安全身份字段收敛现有 continuation。"""

        if not isinstance(observation, Mapping):
            return "ignored", None
        pending_data = observation.get("pending")
        if isinstance(pending_data, Mapping):
            try:
                pending = ClaudeCodePendingInteraction.from_safe_dict(pending_data)
            except (TypeError, ValueError):
                return "conflict", self.get(str(pending_data.get("owner", "")))
            observed_owner = observation.get("owner")
            observed_conversation = observation.get("conversation_id")
            if (
                observed_owner is not None
                and observed_owner != pending.owner
            ) or (
                observed_conversation is not None
                and observed_conversation != pending.originating_conversation_id
            ):
                return "ignored", self.get(pending.owner)
            current = self.get(pending.owner)
            # 不允许不同 process/round 的 run-local observation 覆盖当前动作。
            if current is not None and (
                current.process_id != pending.process_id
                or current.round_id != pending.round_id
                or current.originating_conversation_id
                != pending.originating_conversation_id
            ):
                return "conflict", current
            try:
                status = self.upsert(pending, replace_existing=True)
            except ClaudeCodeContinuationConflict:
                return "conflict", self.get(pending.owner)
            return status, pending

        clear_data = observation.get("clear_identity")
        if isinstance(clear_data, Mapping):
            owner = str(clear_data.get("owner", ""))
            self.clear_if_identity(
                owner,
                process_id=str(clear_data.get("process_id", "")),
                action_id=str(clear_data.get("action_id", "")),
                round_id=clear_data.get("round_id"),
            )
            return "cleared", self.get(owner)

        current_owner = observation.get("owner")
        if not isinstance(current_owner, str) or not current_owner.strip():
            return "ignored", None
        current = self.get(current_owner)
        if current is None:
            return "ignored", None
        # 只有明确携带同一 process/round，才可清除无 action 的 pending。
        observed_process_id = observation.get("observed_process_id")
        observed_round_id = observation.get("observed_round_id")
        if (
            not isinstance(observed_process_id, str)
            or observed_process_id != current.process_id
            or "observed_round_id" not in observation
            or observed_round_id != current.round_id
        ):
            return "ignored", current
        action_present = observation.get("action_present")
        if action_present is True:
            return "same", current
        if action_present is not False:
            return "same", current
        if bool(observation.get("action_consumed")) or bool(
            observation.get("observed")
        ):
            self.clear(current.owner)
            return "cleared", None
        return "same", current


class ClaudeCodeInteractionSink:
    """Agent run 内部的 ActionRequired 捕获器；只生成安全 continuation 投影。"""

    def __init__(
        self,
        *,
        environment: str,
        owner: str,
        conversation_id: str,
        source_message_id: str | None = None,
        clock: Callable[[], float] = time.time,
        ttl_seconds: float = DEFAULT_CONTINUATION_TTL_SECONDS,
    ) -> None:
        self.environment = _safe_identity(environment, "environment")
        self.owner = _safe_identity(owner, "owner")
        self.conversation_id = _safe_identity(conversation_id, "conversation_id")
        self.source_message_id = source_message_id
        self._clock = clock
        self._ttl_seconds = min(float(ttl_seconds), 3600.0)
        self._pending: ClaudeCodePendingInteraction | None = None
        self._clear_identity: dict | None = None
        self._delivery_unknown = False
        self._result_status: dict[str, object] = {}

    def capture_controller_result(self, result) -> None:
        """捕获 Controller 公共结果中的安全 action；不读取私有字段。"""

        action = getattr(result, "action_required", None)
        process_id = getattr(result, "process_id", None)
        state = getattr(getattr(result, "state", None), "value", getattr(result, "state", None))
        outcome = getattr(getattr(result, "outcome", None), "value", getattr(result, "outcome", None))
        self._result_status = {
            "owner": self.owner,
            "conversation_id": self.conversation_id,
            "state": str(state) if state is not None else None,
            "outcome": str(outcome) if outcome is not None else None,
            "process_active": bool(getattr(result, "process_active", False)),
            "round_terminal": bool(getattr(result, "round_terminal", False)),
            "process_id": process_id,
            "round_id": getattr(result, "round_id", None),
            "observed_process_id": process_id,
            "observed_round_id": getattr(result, "round_id", None),
        }
        action_id = getattr(action, "action_id", None) if action is not None else None
        action_process = getattr(action, "process_id", None) if action is not None else None
        action_owner = getattr(action, "session_owner", None) if action is not None else None
        action_kind = getattr(
            getattr(action, "kind", None),
            "value",
            getattr(action, "kind", None),
        )
        process_active = bool(getattr(result, "process_active", False))
        action_is_valid = bool(
            action is not None
            and isinstance(process_id, str)
            and process_id
            and isinstance(action_id, str)
            and action_id
            and action_process == process_id
            and action_owner == self.owner
            and str(action_kind) != "stalled"
            and process_active
        )
        self._result_status["action_present"] = action_is_valid
        self._result_status["action_consumed"] = False
        if action is not None and not action_is_valid:
            action = None
        if (
            action is None
            or not isinstance(process_id, str)
            or not process_id
            or not isinstance(action_id, str)
            or not action_id
        ):
            if self._pending is not None:
                self._clear_identity = {
                    "owner": self.owner,
                    "process_id": self._pending.process_id,
                    "action_id": self._pending.action_id,
                    "round_id": self._pending.round_id,
                }
            self._pending = None
            self._delivery_unknown = bool(getattr(result, "delivery_unknown", False))
            return
        snapshot = getattr(result, "snapshot", None)
        session_ref = getattr(snapshot, "session_ref", None)
        cwd = getattr(session_ref, "cwd", None)
        now = float(self._clock())
        kind = getattr(getattr(action, "kind", None), "value", getattr(action, "kind", "unknown_prompt"))
        self._pending = ClaudeCodePendingInteraction(
            owner=self.owner,
            environment=self.environment,
            originating_conversation_id=self.conversation_id,
            process_id=process_id,
            round_id=getattr(result, "round_id", None),
            action_id=action_id,
            kind=str(kind),
            safe_summary=getattr(action, "summary", "Claude Code requires input"),
            safe_prompt=getattr(action, "prompt_text", ""),
            safe_options=tuple(getattr(action, "options", ()) or ()),
            created_at=now,
            expires_at=now + self._ttl_seconds,
            cwd=cwd,
            source_message_id=self.source_message_id,
        )
        self._clear_identity = None
        self._delivery_unknown = False

    def mark_delivery_unknown(self) -> None:
        self._delivery_unknown = True
        if self._pending is not None:
            self._pending = ClaudeCodePendingInteraction(
                **{
                    field: getattr(self._pending, field)
                    for field in self._pending.__dataclass_fields__
                    if field != "delivery_unknown"
                },
                delivery_unknown=True,
            )

    def snapshot(self) -> dict:
        result: dict = {"observed": True, "delivery_unknown": self._delivery_unknown}
        result.update(self._result_status)
        if self._pending is not None:
            result["pending"] = self._pending.to_safe_dict()
        elif self._clear_identity is not None:
            result["clear_identity"] = dict(self._clear_identity)
        return result


def render_claude_code_interaction(pending: ClaudeCodePendingInteraction) -> str:
    """将安全 action 投影为用户可读提示，不附带原生终端缓冲。"""

    lines = [pending.safe_summary]
    if pending.safe_prompt:
        lines.append(pending.safe_prompt)
    if pending.safe_options:
        lines.append("选项：" + " / ".join(pending.safe_options))
    if pending.delivery_unknown:
        lines.append("上一条回复送达状态未知，请先使用 poll/interrupt/terminate 确认。")
    else:
        lines.append("请直接回复你的选择；不会自动批准或重试。")
    return "\n".join(lines)[:MAX_SAFE_PROMPT + MAX_SAFE_SUMMARY]


def safe_observation_from_controller_result(
    result,
    *,
    environment: str,
    owner: str,
    conversation_id: str,
    source_message_id: str | None = None,
    operation: str = "tool",
) -> dict:
    """把一次 Controller 公共结果转换为同样的安全 sink 观察。"""

    sink = ClaudeCodeInteractionSink(
        environment=environment,
        owner=owner,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
    )
    sink.capture_controller_result(result)
    observation = sink.snapshot()
    if operation == "reply":
        observation["action_consumed"] = (
            "pending" not in observation
            and not bool(observation.get("delivery_unknown"))
        )
        if "pending" in observation:
            observation["outcome"] = "awaiting_claude_code_interaction"
        elif observation.get("round_terminal"):
            observation["outcome"] = "claude_code_terminal"
        else:
            observation["outcome"] = "claude_code_reply_delivered"
    return observation


__all__ = [
    "ClaudeCodeContinuationConflict",
    "ClaudeCodeContinuationStore",
    "ClaudeCodeInteractionSink",
    "ClaudeCodePendingInteraction",
    "DEFAULT_CONTINUATION_TTL_SECONDS",
    "classify_continuation_error",
    "is_identity_stale_error",
    "render_claude_code_interaction",
    "safe_observation_from_controller_result",
]
