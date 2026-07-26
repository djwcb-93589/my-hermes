"""Web 管理 API 的集中只读适配层。"""

from __future__ import annotations

import math
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Iterator

from pydantic import ValidationError
import yaml

from hermes.persistence.core import (
    get_session_messages,
    list_cli_sessions,
    session_exists,
)
from hermes.persistence.cron import get_cron_job, list_cron_jobs, list_cron_runs
from hermes.persistence.database import DBError
from hermes.persistence.read_only import database_is_readable, readonly_connection

from hermes.web.schemas import (
    CronJobDetailResponse,
    CronJobListResponse,
    CronJobSummary,
    CronRunSummary,
    MessageDetail,
    SessionDetailResponse,
    SessionListResponse,
    SessionSummary,
    SkillListResponse,
    SkillSummary,
    StatusResponse,
    ToolsetListResponse,
)


class ReadDataUnavailable(Exception):
    """现有公开接口不足以保证无副作用读取时使用的受控异常。"""


class ResourceNotFound(Exception):
    """为以后可安全启用的只读资源保留的受控未找到异常。"""


class ReadService:
    """把现有公开读取能力转换为 Web schema，不保存业务状态。"""

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path

    def get_status(self) -> StatusResponse:
        """返回不触发 Agent、Gateway 或调度器的进程状态。"""
        try:
            project_version: str | None = version("my-hermes")
        except PackageNotFoundError:
            project_version = None

        database_status = "unavailable"
        if self._db_path:
            try:
                if database_is_readable(self._db_path):
                    database_status = "available"
            except (sqlite3.Error, OSError, ValueError):
                pass

        # Gateway 的可读运行状态没有不竞争 lease 的公开接口。
        return StatusResponse(
            application_name="MyHermes",
            project_version=project_version,
            web_status="running",
            gateway_status="unavailable",
            database_status=database_status,
            current_time=datetime.now(UTC),
        )

    def list_sessions(self, *, limit: int, offset: int) -> SessionListResponse:
        """读取已有 CLI 会话摘要，不补查额外字段。"""
        try:
            with self._connection() as conn:
                records = list_cli_sessions(conn, limit=limit, offset=offset)
            items = [
                SessionSummary(
                    conversation_id=self._required_text(record, "session_id"),
                    preview=self._optional_text(record.get("preview")),
                    source="cli",
                    created_at=None,
                    updated_at=self._timestamp(record.get("timestamp")),
                    message_count=None,
                )
                for record in records
            ]
            return SessionListResponse(items=items, limit=limit, offset=offset)
        except (OSError, TypeError, ValueError, OverflowError, ValidationError) as exc:
            raise ReadDataUnavailable("会话数据当前不可读取。") from exc

    def get_session(self, conversation_id: str) -> SessionDetailResponse:
        """读取已有 CLI 会话消息，不补查消息时间或内部状态。"""
        try:
            with self._connection() as conn:
                if not session_exists(conn, conversation_id, source="cli"):
                    raise ResourceNotFound()
                records = get_session_messages(conn, conversation_id)
            messages = [self._message_detail(record) for record in records]
            return SessionDetailResponse(
                conversation_id=conversation_id,
                source="cli",
                created_at=None,
                updated_at=None,
                messages=messages,
            )
        except (OSError, TypeError, ValueError, ValidationError) as exc:
            raise ReadDataUnavailable("会话数据当前不可读取。") from exc

    def list_cron_jobs(self) -> CronJobListResponse:
        """读取持久化的 Cron 定义，不启动调度器或修改调度状态。"""
        try:
            with self._connection() as conn:
                records = list_cron_jobs(conn)
            return CronJobListResponse(
                items=[self._cron_job_summary(record) for record in records]
            )
        except (OSError, TypeError, ValueError, OverflowError, ValidationError) as exc:
            raise ReadDataUnavailable("Cron 数据当前不可读取。") from exc

    def get_cron_job(self, job_id: str) -> CronJobDetailResponse:
        """读取 Cron 任务定义及已有公开运行历史。"""
        try:
            with self._connection() as conn:
                record = get_cron_job(conn, job_id)
                if record is None:
                    raise ResourceNotFound()
                run_records = list_cron_runs(conn, job_id)
            summary = self._cron_job_summary(record)
            return CronJobDetailResponse(
                job_id=summary.job_id,
                name=summary.name,
                prompt_preview=summary.prompt_preview,
                schedule=summary.schedule,
                timezone=summary.timezone,
                enabled=summary.enabled,
                last_run_at=summary.last_run_at,
                next_run_at=summary.next_run_at,
                runs=[self._cron_run_summary(item) for item in run_records],
            )
        except (OSError, TypeError, ValueError, OverflowError, ValidationError) as exc:
            raise ReadDataUnavailable("Cron 数据当前不可读取。") from exc

    def list_skills(self) -> SkillListResponse:
        """复用现有 Skill 发现逻辑，只映射非敏感摘要。"""
        try:
            from hermes.tools.skill import discover_skills

            discovered = discover_skills()
        except (OSError, yaml.YAMLError) as exc:
            raise ReadDataUnavailable("Skill 目录当前不可读取。") from exc

        items = [
            SkillSummary(
                name=str(item.get("name", "")),
                description=self._optional_text(item.get("description")),
                version=self._optional_text(item.get("version")),
                available="error" not in item,
            )
            for item in discovered
            if item.get("name")
        ]
        return SkillListResponse(items=items)

    def list_toolsets(self) -> ToolsetListResponse:
        """保留 Toolset 响应契约，但拒绝通过注册行为读取元数据。"""
        # 当前项目没有无需注册工具即可读取 Toolset 元数据的公开纯读取接口。
        # 因此不能导入或装配 ToolRegistry，更不能读取其私有字段。
        raise ReadDataUnavailable(
            "当前缺少无副作用的 Toolset 元数据读取接口。"
        )

    @staticmethod
    def _optional_text(value: object) -> str | None:
        """避免把内部对象的表示直接带入 HTTP 响应。"""
        return value if isinstance(value, str) and value else None

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """为单次查询提供连接，并把预期持久化错误转换为受控异常。"""
        if not self._db_path:
            raise ReadDataUnavailable("数据库只读连接当前不可用。")
        try:
            with readonly_connection(self._db_path) as conn:
                yield conn
        except (sqlite3.Error, DBError, OSError) as exc:
            raise ReadDataUnavailable("数据库只读查询当前不可用。") from exc

    @staticmethod
    def _timestamp(value: object) -> datetime | None:
        """将公开的 Unix 时间戳转换为带 UTC 时区的时间。"""
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("timestamp is invalid")
        timestamp = float(value)
        if not math.isfinite(timestamp):
            raise ValueError("timestamp is invalid")
        return datetime.fromtimestamp(timestamp, UTC)

    @staticmethod
    def _required_text(record: dict, field_name: str) -> str:
        """确保领域记录中的标识字段不会以内部对象形式泄漏。"""
        value = record.get(field_name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field_name} is invalid")
        return value

    def _message_detail(self, record: dict) -> MessageDetail:
        """丢弃消息字典中不属于 Web schema 的内部字段。"""
        tool_calls = record.get("tool_calls")
        if tool_calls is not None and (
            not isinstance(tool_calls, list)
            or any(not isinstance(item, dict) for item in tool_calls)
        ):
            raise ValueError("tool_calls is invalid")
        return MessageDetail(
            role=self._required_text(record, "role"),
            content=self._message_content(record),
            tool_calls=tool_calls,
            tool_call_id=self._optional_text(record.get("tool_call_id")),
            timestamp=None,
        )

    @staticmethod
    def _message_content(record: dict) -> str:
        """保留正常的空消息内容，但拒绝非字符串内部对象。"""
        content = record.get("content")
        if not isinstance(content, str):
            raise ValueError("content is invalid")
        return content

    def _cron_job_summary(self, record: dict) -> CronJobSummary:
        """将公开 Cron 定义转换为不包含能力和投递信息的摘要。"""
        schedule_type = self._required_text(record, "schedule_type")
        schedule_expr = self._required_text(record, "schedule_expr")
        prompt = self._required_text(record, "prompt")
        normalized_prompt = " ".join(prompt.split())
        preview = (
            f"{normalized_prompt[:117]}..."
            if len(normalized_prompt) > 120
            else normalized_prompt
        )
        paused = record.get("paused")
        deleted_at = record.get("deleted_at")
        if not isinstance(paused, bool):
            raise ValueError("paused is invalid")
        return CronJobSummary(
            job_id=self._required_text(record, "job_id"),
            name=self._required_text(record, "name"),
            prompt_preview=preview,
            schedule=f"{schedule_type}:{schedule_expr}",
            timezone=self._optional_text(record.get("timezone")),
            enabled=not paused and deleted_at is None,
            last_run_at=self._timestamp(record.get("last_run_at")),
            next_run_at=self._timestamp(record.get("next_run_at")),
        )

    def _cron_run_summary(self, record: dict) -> CronRunSummary:
        """只映射公开运行历史中适合管理页面展示的字段。"""
        return CronRunSummary(
            run_id=self._required_text(record, "run_id"),
            status=self._required_text(record, "status"),
            scheduled_for=self._timestamp(record.get("scheduled_for")),
            started_at=self._timestamp(record.get("started_at")),
            finished_at=self._timestamp(record.get("finished_at")),
            result_summary=self._optional_text(record.get("result_summary")),
        )
