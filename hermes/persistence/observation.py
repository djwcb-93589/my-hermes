"""Observation 中立契约的 SQLite 写入实现。"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from hermes.observability.contracts import (
    ModelCallObservation,
    RunObservation,
    ToolCallObservation,
)
from hermes.observability.hooks import register_observation_sink
from hermes.redaction import redact_explicit_secrets

from .database import DBError, _immediate_transaction
from .schema import init_db
from .write_existing import existing_write_connection

if TYPE_CHECKING:
    from hermes.hooks import (
        AsyncHookRegistry,
        HookRegistration,
        SyncHookRegistry,
    )


class ObservationConflictError(DBError):
    """同一 Observation 标识已被不同安全事件占用。"""


@dataclass(frozen=True, slots=True)
class _PersistedObservation:
    """与数据库事件列一一对应的内部不可变写入投影。"""

    observation_id: str
    event_type: str
    run_id: str
    parent_run_id: str | None
    tool_call_id: str | None = None
    tool_name: str | None = None
    status: str | None = None
    success: int | None = None
    error_type: str | None = None
    finish_reason: str | None = None
    has_text: int | None = None
    tool_call_count: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None
    duration_ms: int | None = None
    stop_reason: str | None = None
    iterations: int | None = None
    has_final_reply: int | None = None

    def event_values(self) -> tuple[object, ...]:
        """返回除标识和首次写入时间之外的完整事件派生字段。"""
        return (
            self.event_type,
            self.run_id,
            self.parent_run_id,
            self.tool_call_id,
            self.tool_name,
            self.status,
            self.success,
            self.error_type,
            self.finish_reason,
            self.has_text,
            self.tool_call_count,
            self.prompt_tokens,
            self.completion_tokens,
            self.total_tokens,
            self.prompt_cache_hit_tokens,
            self.prompt_cache_miss_tokens,
            self.duration_ms,
            self.stop_reason,
            self.iterations,
            self.has_final_reply,
        )


_EVENT_COLUMNS = (
    "event_type, run_id, parent_run_id, tool_call_id, tool_name, status, "
    "success, error_type, finish_reason, has_text, tool_call_count, "
    "prompt_tokens, completion_tokens, total_tokens, "
    "prompt_cache_hit_tokens, prompt_cache_miss_tokens, duration_ms, "
    "stop_reason, iterations, has_final_reply"
)
_MAX_PERSISTED_IDENTIFIER_LENGTH = 256
_MAX_PERSISTED_LABEL_LENGTH = 128
_PERSISTED_IDENTIFIER_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}$"
)
_PERSISTED_LABEL_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
)
_CONTROL_TEXT_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _persisted_text(
    value: object,
    field_name: str,
    *,
    label: bool = False,
) -> str:
    """校验 Writer 自身的紧凑字段边界，不依赖读取展示模型。"""
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"observation {field_name} is invalid")
    limit = (
        _MAX_PERSISTED_LABEL_LENGTH
        if label
        else _MAX_PERSISTED_IDENTIFIER_LENGTH
    )
    pattern = _PERSISTED_LABEL_RE if label else _PERSISTED_IDENTIFIER_RE
    if (
        len(value) > limit
        or _CONTROL_TEXT_RE.search(value)
        or not pattern.fullmatch(value)
        or value.startswith(("/", "\\"))
        or _WINDOWS_ABSOLUTE_PATH_RE.match(value)
        or redact_explicit_secrets(value) != value
    ):
        raise ValueError(f"observation {field_name} is invalid")
    return value


def _optional_persisted_text(
    value: object,
    field_name: str,
    *,
    label: bool = False,
) -> str | None:
    if value is None:
        return None
    return _persisted_text(value, field_name, label=label)


def _persisted_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"observation {field_name} must be a boolean")
    return value


def _persisted_count(value: object, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(
            f"observation {field_name} must be a non-negative integer"
        )
    return value


def _optional_persisted_count(
    value: object,
    field_name: str,
) -> int | None:
    if value is None:
        return None
    return _persisted_count(value, field_name)


class SQLiteObservationSink:
    """每次事件使用独立连接持久化安全 Observation 字段。"""

    __slots__ = ("_db_path",)

    def __init__(self, db_path: str | Path):
        if not isinstance(db_path, (str, Path)):
            raise TypeError("db_path must be a path")
        normalized = str(db_path)
        if not normalized.strip():
            raise ValueError("db_path must be a non-empty path")
        self._db_path = normalized

    def record_tool_call(self, observation: ToolCallObservation) -> None:
        """持久化工具调用摘要，不保存参数或结果。"""
        if not isinstance(observation, ToolCallObservation):
            raise TypeError("observation must be a ToolCallObservation")
        self._record(
            _PersistedObservation(
                observation_id=_persisted_text(
                    observation.observation_id,
                    "observation_id",
                ),
                event_type="tool_call",
                run_id=_persisted_text(observation.run_id, "run_id"),
                parent_run_id=_optional_persisted_text(
                    observation.parent_run_id,
                    "parent_run_id",
                ),
                tool_call_id=_persisted_text(
                    observation.tool_call_id,
                    "tool_call_id",
                ),
                tool_name=_persisted_text(
                    observation.tool_name,
                    "tool_name",
                    label=True,
                ),
                status=_persisted_text(
                    observation.status,
                    "status",
                    label=True,
                ),
                success=int(
                    _persisted_bool(observation.success, "success")
                ),
                error_type=_optional_persisted_text(
                    observation.error_type,
                    "error_type",
                    label=True,
                ),
                duration_ms=_persisted_count(
                    observation.duration_ms,
                    "duration_ms",
                ),
            )
        )

    def record_model_call(self, observation: ModelCallObservation) -> None:
        """持久化模型调用计数，不保存 Prompt 或模型输出。"""
        if not isinstance(observation, ModelCallObservation):
            raise TypeError("observation must be a ModelCallObservation")
        self._record(
            _PersistedObservation(
                observation_id=_persisted_text(
                    observation.observation_id,
                    "observation_id",
                ),
                event_type="model_call",
                run_id=_persisted_text(observation.run_id, "run_id"),
                parent_run_id=_optional_persisted_text(
                    observation.parent_run_id,
                    "parent_run_id",
                ),
                finish_reason=_optional_persisted_text(
                    observation.finish_reason,
                    "finish_reason",
                    label=True,
                ),
                has_text=int(
                    _persisted_bool(observation.has_text, "has_text")
                ),
                tool_call_count=_persisted_count(
                    observation.tool_call_count,
                    "tool_call_count",
                ),
                prompt_tokens=_optional_persisted_count(
                    observation.prompt_tokens,
                    "prompt_tokens",
                ),
                completion_tokens=_optional_persisted_count(
                    observation.completion_tokens,
                    "completion_tokens",
                ),
                total_tokens=_optional_persisted_count(
                    observation.total_tokens,
                    "total_tokens",
                ),
                prompt_cache_hit_tokens=_optional_persisted_count(
                    observation.prompt_cache_hit_tokens,
                    "prompt_cache_hit_tokens",
                ),
                prompt_cache_miss_tokens=_optional_persisted_count(
                    observation.prompt_cache_miss_tokens,
                    "prompt_cache_miss_tokens",
                ),
                duration_ms=_persisted_count(
                    observation.duration_ms,
                    "duration_ms",
                ),
            )
        )

    def record_run_end(self, observation: RunObservation) -> None:
        """持久化运行结束摘要，不保存最终回答。"""
        if not isinstance(observation, RunObservation):
            raise TypeError("observation must be a RunObservation")
        self._record(
            _PersistedObservation(
                observation_id=_persisted_text(
                    observation.observation_id,
                    "observation_id",
                ),
                event_type="run_end",
                run_id=_persisted_text(observation.run_id, "run_id"),
                parent_run_id=_optional_persisted_text(
                    observation.parent_run_id,
                    "parent_run_id",
                ),
                status=_persisted_text(
                    observation.status,
                    "status",
                    label=True,
                ),
                tool_call_count=_persisted_count(
                    observation.tool_call_count,
                    "tool_call_count",
                ),
                stop_reason=_persisted_text(
                    observation.stop_reason,
                    "stop_reason",
                    label=True,
                ),
                iterations=_persisted_count(
                    observation.iterations,
                    "iterations",
                ),
                has_final_reply=int(
                    _persisted_bool(
                        observation.has_final_reply,
                        "has_final_reply",
                    )
                ),
            )
        )

    def _record(self, record: _PersistedObservation) -> None:
        """在同一写事务中完成冲突读取、全字段比较和首次插入。"""
        try:
            with existing_write_connection(self._db_path) as conn:
                with _immediate_transaction(conn):
                    existing = conn.execute(
                        f"""
                        SELECT {_EVENT_COLUMNS}
                        FROM observations
                        WHERE observation_id=?
                        """,
                        (record.observation_id,),
                    ).fetchone()
                    if existing is not None:
                        if tuple(existing) != record.event_values():
                            raise ObservationConflictError(
                                "observation idempotency conflict"
                            )
                        return
                    conn.execute(
                        f"""
                        INSERT INTO observations (
                            observation_id, {_EVENT_COLUMNS}, created_at
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        (
                            record.observation_id,
                            *record.event_values(),
                            time.time(),
                        ),
                    )
        except ObservationConflictError:
            raise
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise DBError("observation persistence failed") from exc


def configure_sqlite_observation_sink(
    hook_registry: SyncHookRegistry | AsyncHookRegistry,
    db_path: str | Path,
    *,
    hook_id_prefix: str = "sqlite_observation",
) -> tuple[HookRegistration, ...]:
    """初始化最新 schema，并把单一 SQLite Sink 幂等装配到 Hook Registry。"""
    from hermes.hooks import (
        AsyncHookRegistry,
        HookEventName,
        HookRegistrationError,
        SyncHookRegistry,
    )

    if not isinstance(hook_registry, (SyncHookRegistry, AsyncHookRegistry)):
        raise TypeError("hook_registry must be a HookRegistry")
    if not isinstance(db_path, (str, Path)) or not str(db_path).strip():
        raise ValueError("db_path must be a non-empty path")
    if not isinstance(hook_id_prefix, str) or not hook_id_prefix.strip():
        raise ValueError("hook_id_prefix must be a non-empty string")
    prefix = hook_id_prefix.strip()
    event_suffixes = (
        (HookEventName.POST_TOOL_CALL.value, "post_tool_call"),
        (HookEventName.POST_LLM_CALL.value, "post_llm_call"),
        (HookEventName.RUN_END.value, "run_end"),
    )
    existing = []
    for event_name, suffix in event_suffixes:
        expected_id = f"{prefix}:{suffix}"
        matching = tuple(
            registration
            for registration in hook_registry.registered_hooks(event_name)
            if registration.hook_id == expected_id
        )
        existing.extend(matching)
    if len(existing) == len(event_suffixes):
        return tuple(existing)
    if existing:
        raise HookRegistrationError(
            "SQLite observation sink hook registration is incomplete"
        )

    connection = init_db(str(db_path))
    connection.close()
    return register_observation_sink(
        hook_registry,
        SQLiteObservationSink(db_path),
        hook_id_prefix=prefix,
    )


__all__ = [
    "ObservationConflictError",
    "SQLiteObservationSink",
    "configure_sqlite_observation_sink",
]
