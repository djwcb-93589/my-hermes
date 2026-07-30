"""Observation 中立契约的 SQLite 写入实现。"""

from __future__ import annotations

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
from hermes.observability.monitoring import (
    ModelCallObservationView,
    ObservationEventType,
    RunObservationView,
    ToolCallObservationView,
)

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
            self.duration_ms,
            self.stop_reason,
            self.iterations,
            self.has_final_reply,
        )


_EVENT_COLUMNS = (
    "event_type, run_id, parent_run_id, tool_call_id, tool_name, status, "
    "success, error_type, finish_reason, has_text, tool_call_count, "
    "prompt_tokens, completion_tokens, total_tokens, duration_ms, "
    "stop_reason, iterations, has_final_reply"
)
# 写入时间在首次插入时生成；此固定值仅用于复用中立安全投影校验。
_SAFE_PROJECTION_TIMESTAMP = 0.0


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
        safe = ToolCallObservationView(
            observation_id=observation.observation_id,
            event_type=ObservationEventType.TOOL_CALL,
            run_id=observation.run_id,
            parent_run_id=observation.parent_run_id,
            created_at=_SAFE_PROJECTION_TIMESTAMP,
            tool_call_id=observation.tool_call_id,
            tool_name=observation.tool_name,
            status=observation.status,
            success=observation.success,
            error_type=observation.error_type,
            duration_ms=observation.duration_ms,
        )
        self._record(
            _PersistedObservation(
                observation_id=safe.observation_id,
                event_type="tool_call",
                run_id=safe.run_id,
                parent_run_id=safe.parent_run_id,
                tool_call_id=safe.tool_call_id,
                tool_name=safe.tool_name,
                status=safe.status,
                success=int(safe.success),
                error_type=safe.error_type,
                duration_ms=safe.duration_ms,
            )
        )

    def record_model_call(self, observation: ModelCallObservation) -> None:
        """持久化模型调用计数，不保存 Prompt 或模型输出。"""
        if not isinstance(observation, ModelCallObservation):
            raise TypeError("observation must be a ModelCallObservation")
        safe = ModelCallObservationView(
            observation_id=observation.observation_id,
            event_type=ObservationEventType.MODEL_CALL,
            run_id=observation.run_id,
            parent_run_id=observation.parent_run_id,
            created_at=_SAFE_PROJECTION_TIMESTAMP,
            finish_reason=observation.finish_reason,
            has_text=observation.has_text,
            tool_call_count=observation.tool_call_count,
            prompt_tokens=observation.prompt_tokens,
            completion_tokens=observation.completion_tokens,
            total_tokens=observation.total_tokens,
            duration_ms=observation.duration_ms,
        )
        self._record(
            _PersistedObservation(
                observation_id=safe.observation_id,
                event_type="model_call",
                run_id=safe.run_id,
                parent_run_id=safe.parent_run_id,
                finish_reason=safe.finish_reason,
                has_text=int(safe.has_text),
                tool_call_count=safe.tool_call_count,
                prompt_tokens=safe.prompt_tokens,
                completion_tokens=safe.completion_tokens,
                total_tokens=safe.total_tokens,
                duration_ms=safe.duration_ms,
            )
        )

    def record_run_end(self, observation: RunObservation) -> None:
        """持久化运行结束摘要，不保存最终回答。"""
        if not isinstance(observation, RunObservation):
            raise TypeError("observation must be a RunObservation")
        safe = RunObservationView(
            observation_id=observation.observation_id,
            event_type=ObservationEventType.RUN_END,
            run_id=observation.run_id,
            parent_run_id=observation.parent_run_id,
            created_at=_SAFE_PROJECTION_TIMESTAMP,
            status=observation.status,
            stop_reason=observation.stop_reason,
            iterations=observation.iterations,
            tool_call_count=observation.tool_call_count,
            has_final_reply=observation.has_final_reply,
        )
        self._record(
            _PersistedObservation(
                observation_id=safe.observation_id,
                event_type="run_end",
                run_id=safe.run_id,
                parent_run_id=safe.parent_run_id,
                status=safe.status,
                tool_call_count=safe.tool_call_count,
                stop_reason=safe.stop_reason,
                iterations=safe.iterations,
                has_final_reply=int(safe.has_final_reply),
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
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
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
