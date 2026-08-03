"""Claude Code 终态通知的跨平台安全合同。"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from hermes.claude_code.contracts import (
    CLAUDE_CODE_PROCESS_STATUSES,
    ClaudeCodeState,
)


_MAX_TARGET_ID_CHARS = 512
_MAX_TARGET_METADATA_BYTES = 8_192
_MAX_NOTIFICATION_ID_CHARS = 512
_MAX_WATCH_ID_CHARS = 512
_MAX_PROCESS_ID_CHARS = 512
_MAX_SESSION_OWNER_CHARS = 1_024
_MAX_CWD_CHARS = 8_192
_MAX_SAFE_OUTPUT_TAIL_CHARS = 16_384
_MAX_LIMITS_HIT = 64
_TERMINAL_STATES = frozenset(
    {
        ClaudeCodeState.COMPLETED,
        ClaudeCodeState.FAILED,
        ClaudeCodeState.INTERRUPTED,
        ClaudeCodeState.LOST,
    }
)
_SENSITIVE_METADATA_KEY_PARTS = frozenset(
    {
        "api_key",
        "authorization",
        "credential",
        "password",
        "secret",
        "token",
    }
)


def _require_nonempty_text(
    field_name: str,
    value: object,
    *,
    maximum: int,
) -> str:
    """校验有限的跨边界标识，避免意外保存大段输入。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if len(value) > maximum:
        raise ValueError(f"{field_name} exceeds the supported length")
    return value


def _contains_sensitive_metadata_key(value: object) -> bool:
    """目标元数据只允许投递定位信息，不接受显式凭据字段。"""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                return True
            normalized_key = key.lower().replace("-", "_")
            if any(part in normalized_key for part in _SENSITIVE_METADATA_KEY_PARTS):
                return True
            if _contains_sensitive_metadata_key(nested):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_sensitive_metadata_key(item) for item in value)
    return False


def _freeze_target_metadata(metadata: Mapping[str, object]) -> Mapping[str, object]:
    """复制并冻结受限 JSON 元数据，隔离调用方后续修改。"""

    if not isinstance(metadata, Mapping):
        raise ValueError("metadata must be a mapping")
    if _contains_sensitive_metadata_key(metadata):
        raise ValueError("metadata must not contain credential fields")
    try:
        prepared = _copy_json_value(metadata)
        serialized = json.dumps(
            prepared,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        copied = json.loads(serialized)
    except (TypeError, ValueError) as exc:
        raise ValueError("metadata must be JSON-compatible") from exc
    if not isinstance(copied, dict) or not all(
        isinstance(key, str) for key in copied
    ):
        raise ValueError("metadata keys must be strings")
    if len(serialized.encode("utf-8")) > _MAX_TARGET_METADATA_BYTES:
        raise ValueError("metadata exceeds the supported size")
    frozen = _freeze_json_value(copied)
    if not isinstance(frozen, Mapping):
        raise ValueError("metadata must be a mapping")
    return frozen


def _copy_json_value(value: object) -> object:
    """将任意 Mapping/tuple 规整为可校验的普通 JSON 容器。"""

    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("metadata keys must be strings")
        return {
            key: _copy_json_value(nested)
            for key, nested in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_copy_json_value(item) for item in value]
    return value


def _freeze_json_value(value: object) -> object:
    """递归冻结已验证的 JSON 值，避免嵌套元数据被调用方随后改写。"""

    if isinstance(value, dict):
        return MappingProxyType(
            {
                key: _freeze_json_value(nested)
                for key, nested in value.items()
            }
        )
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _require_timestamp(field_name: str, value: object) -> float:
    """校验完成时刻，拒绝无穷值和布尔值。"""

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{field_name} must be a non-negative timestamp")
    return float(value)


@dataclass(frozen=True, slots=True)
class ClaudeCodeNotificationTarget:
    """Watcher 只传递的不透明投递目标，不解释平台细节。"""

    target_id: str
    metadata: Mapping[str, object] = field(repr=False)
    session_owner: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_id",
            _require_nonempty_text(
                "target_id",
                self.target_id,
                maximum=_MAX_TARGET_ID_CHARS,
            ),
        )
        object.__setattr__(
            self,
            "metadata",
            _freeze_target_metadata(self.metadata),
        )
        if self.session_owner is not None:
            object.__setattr__(
                self,
                "session_owner",
                _require_nonempty_text(
                    "session_owner",
                    self.session_owner,
                    maximum=_MAX_SESSION_OWNER_CHARS,
                ),
            )


@dataclass(frozen=True, slots=True)
class ClaudeCodeTerminalNotification:
    """只含公共安全 Snapshot 的一次终态通知请求。"""

    notification_id: str
    watch_id: str
    process_id: str
    session_owner: str
    cwd: str
    terminal_state: ClaudeCodeState
    controller_outcome: str
    process_status: str | None
    exit_code: int | None
    completed_at: float
    safe_output_tail: str = field(repr=False)
    limits_hit: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name, value, maximum in (
            ("notification_id", self.notification_id, _MAX_NOTIFICATION_ID_CHARS),
            ("watch_id", self.watch_id, _MAX_WATCH_ID_CHARS),
            ("process_id", self.process_id, _MAX_PROCESS_ID_CHARS),
            ("session_owner", self.session_owner, _MAX_SESSION_OWNER_CHARS),
            ("cwd", self.cwd, _MAX_CWD_CHARS),
            ("controller_outcome", self.controller_outcome, 128),
        ):
            object.__setattr__(
                self,
                field_name,
                _require_nonempty_text(field_name, value, maximum=maximum),
            )
        if self.terminal_state not in _TERMINAL_STATES:
            raise ValueError("terminal_state must be a Claude Code terminal state")
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
        object.__setattr__(
            self,
            "completed_at",
            _require_timestamp("completed_at", self.completed_at),
        )
        if not isinstance(self.safe_output_tail, str):
            raise ValueError("safe_output_tail must be text")
        if len(self.safe_output_tail) > _MAX_SAFE_OUTPUT_TAIL_CHARS:
            raise ValueError("safe_output_tail exceeds the supported length")
        if (
            not isinstance(self.limits_hit, tuple)
            or len(self.limits_hit) > _MAX_LIMITS_HIT
            or any(
                not isinstance(item, str) or not item or len(item) > 128
                for item in self.limits_hit
            )
        ):
            raise ValueError("limits_hit must contain bounded non-empty strings")


@dataclass(frozen=True, slots=True)
class ClaudeCodeNotificationReceipt:
    """NotificationPort 的接收结果，不把平台送达误报为同步成功。"""

    accepted: bool
    notification_id: str
    delivery_id: str | None = None
    retryable: bool = False
    error_type: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise ValueError("accepted must be a boolean")
        _require_nonempty_text(
            "notification_id",
            self.notification_id,
            maximum=_MAX_NOTIFICATION_ID_CHARS,
        )
        if self.delivery_id is not None:
            _require_nonempty_text(
                "delivery_id",
                self.delivery_id,
                maximum=_MAX_NOTIFICATION_ID_CHARS,
            )
        if self.accepted and self.delivery_id is None:
            raise ValueError("accepted receipts require a delivery_id")
        if not isinstance(self.retryable, bool):
            raise ValueError("retryable must be a boolean")
        if self.error_type is not None:
            _require_nonempty_text(
                "error_type",
                self.error_type,
                maximum=128,
            )


@runtime_checkable
class ClaudeCodeNotificationPort(Protocol):
    """终态通知的异步平台适配边界。"""

    async def submit_terminal_notification(
        self,
        *,
        target: ClaudeCodeNotificationTarget,
        notification: ClaudeCodeTerminalNotification,
    ) -> ClaudeCodeNotificationReceipt:
        """接收一条终态通知；accepted 代表已进入可靠投递基础设施。"""


def render_claude_code_terminal_notification(
    notification: ClaudeCodeTerminalNotification,
) -> str:
    """用确定性文本渲染通知，不调用模型且不读取原生交互视图。"""

    state = notification.terminal_state
    headings = {
        ClaudeCodeState.COMPLETED: "Claude Code task completed.",
        ClaudeCodeState.FAILED: "Claude Code task failed.",
        ClaudeCodeState.INTERRUPTED: "Claude Code task interrupted.",
        ClaudeCodeState.LOST: (
            "Claude Code task state was lost and can no longer be monitored."
        ),
    }
    lines = [
        headings[state],
        "",
        f"Status: {state.value.lower()}",
        f"Working directory: {notification.cwd}",
    ]
    if state == ClaudeCodeState.FAILED:
        status = notification.process_status or "unknown"
        exit_code = (
            "unknown"
            if notification.exit_code is None
            else str(notification.exit_code)
        )
        lines.append(f"Exit information: status={status}, exit_code={exit_code}")
    elif state == ClaudeCodeState.LOST:
        lines.append(
            "Check the current myHermes runtime and process state before retrying."
        )
    if state != ClaudeCodeState.LOST:
        lines.extend(
            [
                "",
                "Latest safe output:",
                notification.safe_output_tail or "(no safe output available)",
            ]
        )
    return "\n".join(lines)


__all__ = [
    "ClaudeCodeNotificationPort",
    "ClaudeCodeNotificationReceipt",
    "ClaudeCodeNotificationTarget",
    "ClaudeCodeTerminalNotification",
    "render_claude_code_terminal_notification",
]
