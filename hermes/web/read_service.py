"""Dashboard 领域只读服务及兼容门面。"""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from pydantic import ValidationError
import yaml

from hermes.config_values import hermes_home
from hermes.observability.contracts import (
    CapabilityDescriptor,
    ToolsetDescriptor,
)
from hermes.persistence.core import (
    list_cli_session_summaries,
    list_session_message_records_for_dashboard,
    session_exists,
)
from hermes.persistence.cron import get_cron_job, list_cron_jobs, list_cron_runs
from hermes.web.health import inspect_database_health, inspect_gateway_health
from hermes.web.pagination import DEFAULT_PAGE_LIMIT, PageParams, split_page
from hermes.web.read_context import (
    DashboardReadContext,
    DashboardReadError,
    ReadDataUnavailable,
    ResourceNotFound,
)
from hermes.web.redaction import (
    REDACTED_VALUE,
    TRUNCATED_VALUE,
    DashboardRedactor,
)
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
    ToolCapabilitySummary,
    ToolsetListResponse,
    ToolsetSummary,
)


_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_SKILL_FRONTMATTER_CHARS = 16_384
_MISSING = object()
_INVALID_TOOL_CALL_STATUS = "invalid"
_INVALID_TOOL_CALL_DETAIL = REDACTED_VALUE


class _ReadServiceBase:
    """领域读取服务共享的只读上下文、脱敏器和数据校验 helper。"""

    def __init__(
        self,
        context: DashboardReadContext | str | Path | None = None,
        redactor: DashboardRedactor | None = None,
    ):
        if isinstance(context, DashboardReadContext):
            self._context = context
        elif context is None or isinstance(context, (str, Path)):
            self._context = DashboardReadContext(context)
        else:
            raise TypeError("context must be DashboardReadContext, path, or None")
        self._redactor = redactor or DashboardRedactor()

    @staticmethod
    def _timestamp(value: object) -> datetime | None:
        """将公开 Unix 时间戳转换为带 UTC 时区的时间。"""
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("timestamp is invalid")
        timestamp = float(value)
        if not math.isfinite(timestamp):
            raise ValueError("timestamp is invalid")
        return datetime.fromtimestamp(timestamp, UTC)

    @staticmethod
    def _required_text(record: object, field_name: str) -> str:
        """只允许领域记录中的必需文本字段进入响应构造。"""
        if not isinstance(record, dict):
            raise ValueError("record is invalid")
        value = record.get(field_name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field_name} is invalid")
        return value

    @staticmethod
    def _optional_text(value: object) -> str | None:
        """避免将持久化层内部对象表示直接暴露到 HTTP 响应。"""
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("text is invalid")
        return value or None


class HealthReadService(_ReadServiceBase):
    """仅提供 M1 已有的数据库、Gateway 和项目状态读取。"""

    def get_status(self) -> StatusResponse:
        """返回只读数据库与 Gateway lease 状态，不探测真实运行进程。"""
        try:
            project_version: str | None = version("my-hermes")
        except PackageNotFoundError:
            project_version = None

        database = inspect_database_health(self._context.db_path)
        gateway = inspect_gateway_health(self._context.db_path)
        return StatusResponse(
            application_name="MyHermes",
            project_version=project_version,
            web_status="running",
            database=database,
            gateway=gateway,
            # 保留旧字段以兼容已有调用方；新代码应使用嵌套状态模型。
            gateway_status=gateway.status,
            database_status=(
                "available" if database.status == "healthy" else database.status
            ),
            current_time=datetime.now(UTC),
        )


class SessionReadService(_ReadServiceBase):
    """读取 CLI 会话及消息，不访问 Gateway、Agent 或运行时状态。"""

    def list_sessions(self, *, page: PageParams) -> SessionListResponse:
        """用 SQL 分页读取 CLI 会话摘要，再在返回前脱敏。"""
        try:
            with self._context.connection() as conn:
                records = list_cli_session_summaries(
                    conn,
                    limit=page.fetch_limit,
                    offset=page.offset,
                )
            items = [self._session_summary(record) for record in records]
            items, has_more = split_page(items, page)
            return SessionListResponse(
                items=items,
                limit=page.limit,
                offset=page.offset,
                has_more=has_more,
            )
        except (TypeError, ValueError, OverflowError, OSError, ValidationError) as exc:
            raise ReadDataUnavailable("data_invalid") from exc

    def get_session(self, conversation_id: str) -> SessionDetailResponse:
        """读取单个 CLI 会话的安全消息副本。"""
        try:
            with self._context.connection() as conn:
                if not session_exists(conn, conversation_id, source="cli"):
                    raise ResourceNotFound()
                records = list_session_message_records_for_dashboard(
                    conn,
                    conversation_id,
                )
            messages = [self._message_detail(record) for record in records]
            return SessionDetailResponse(
                conversation_id=conversation_id,
                source="cli",
                created_at=None,
                updated_at=None,
                messages=messages,
            )
        except (TypeError, ValueError, OverflowError, OSError, ValidationError) as exc:
            raise ReadDataUnavailable("data_invalid") from exc

    def _session_summary(self, record: object) -> SessionSummary:
        """将持久化的会话摘要转换为有限长度的公开摘要。"""
        if not isinstance(record, dict):
            raise ValueError("record is invalid")
        preview = self._optional_text(record.get("preview"))
        return SessionSummary(
            conversation_id=self._required_text(record, "session_id"),
            preview=(
                self._redactor.preview_text(preview)
                if preview is not None
                else None
            ),
            source="cli",
            created_at=None,
            updated_at=self._timestamp(record.get("timestamp")),
            message_count=None,
        )

    def _message_detail(self, record: object) -> MessageDetail:
        """仅映射消息公开字段，并安全处理损坏的 tool_calls。"""
        if not isinstance(record, dict):
            raise ValueError("record is invalid")
        tool_calls_raw = record.get("tool_calls_raw", _MISSING)
        if tool_calls_raw is _MISSING:
            safe_tool_calls = self._redact_tool_calls(record.get("tool_calls"))
        else:
            safe_tool_calls = self._parse_tool_calls_raw(tool_calls_raw)
        content = record.get("content")
        if not isinstance(content, str):
            raise ValueError("content is invalid")
        return MessageDetail(
            role=self._redactor.structure_text(
                self._required_text(record, "role")
            ),
            content=self._redactor.message_text(content),
            tool_calls=safe_tool_calls,
            tool_call_id=self._optional_text(record.get("tool_call_id")),
            timestamp=None,
        )

    def _parse_tool_calls_raw(
        self,
        tool_calls_raw: object,
    ) -> list[dict[str, object]] | None:
        """安全解析 Dashboard 专用原始字段，解析失败只降级当前消息。"""
        if tool_calls_raw is None:
            return None
        if not isinstance(tool_calls_raw, str):
            return self._invalid_tool_calls_placeholder()
        try:
            tool_calls = json.loads(tool_calls_raw)
        except (TypeError, ValueError, RecursionError):
            return self._invalid_tool_calls_placeholder()
        if not isinstance(tool_calls, list):
            return self._invalid_tool_calls_placeholder()
        return self._redact_tool_calls(tool_calls)

    def _redact_tool_calls(
        self,
        tool_calls: object,
    ) -> list[dict[str, object]] | None:
        """共享预算脱敏工具调用；损坏项只使用固定占位对象替换。"""
        if tool_calls is None:
            return None
        if not isinstance(tool_calls, list):
            return self._invalid_tool_calls_placeholder()
        result: list[dict[str, object]] = []
        try:
            # 非字典项不进入递归脱敏，避免损坏内容消耗整组共享预算。
            values = [item if isinstance(item, dict) else None for item in tool_calls]
            redacted_values = self._redactor.redact_value_list(values)
            for redacted in redacted_values:
                if isinstance(redacted, str) and redacted == TRUNCATED_VALUE:
                    result.append({TRUNCATED_VALUE: TRUNCATED_VALUE})
                    break
                if not isinstance(redacted, dict):
                    result.append(self._invalid_tool_call_item())
                    continue
                result.append(redacted)
        except (TypeError, ValueError, OverflowError, RecursionError):
            return self._invalid_tool_calls_placeholder()
        return result

    @staticmethod
    def _invalid_tool_call_item() -> dict[str, str]:
        """返回新的占位对象，避免调用方修改模块级稳定定义。"""
        return {
            "status": _INVALID_TOOL_CALL_STATUS,
            "detail": _INVALID_TOOL_CALL_DETAIL,
        }

    @classmethod
    def _invalid_tool_calls_placeholder(cls) -> list[dict[str, str]]:
        """返回新的占位列表，保证每个响应互不共享可变对象。"""
        return [cls._invalid_tool_call_item()]


class CronReadService(_ReadServiceBase):
    """读取 Cron 定义和运行历史，不承载任何控制或调度逻辑。"""

    def list_cron_jobs(self, *, page: PageParams) -> CronJobListResponse:
        """用 SQL 分页读取 Cron 定义，不启动调度器或修改调度状态。"""
        try:
            with self._context.connection() as conn:
                records = list_cron_jobs(
                    conn,
                    limit=page.fetch_limit,
                    offset=page.offset,
                )
            items = [self._cron_job_summary(record) for record in records]
            items, has_more = split_page(items, page)
            return CronJobListResponse(
                items=items,
                limit=page.limit,
                offset=page.offset,
                has_more=has_more,
            )
        except (TypeError, ValueError, OverflowError, OSError, ValidationError) as exc:
            raise ReadDataUnavailable("data_invalid") from exc

    def get_cron_job(
        self,
        job_id: str,
        *,
        page: PageParams,
    ) -> CronJobDetailResponse:
        """读取 Cron 任务定义及其分页后的公开运行历史。"""
        try:
            with self._context.connection() as conn:
                record = get_cron_job(conn, job_id)
                if record is None:
                    raise ResourceNotFound()
                run_records = list_cron_runs(
                    conn,
                    job_id,
                    limit=page.fetch_limit,
                    offset=page.offset,
                )
            summary = self._cron_job_summary(record)
            runs = [self._cron_run_summary(item) for item in run_records]
            runs, has_more = split_page(runs, page)
            return CronJobDetailResponse(
                job_id=summary.job_id,
                name=summary.name,
                prompt_preview=summary.prompt_preview,
                schedule=summary.schedule,
                timezone=summary.timezone,
                enabled=summary.enabled,
                last_run_at=summary.last_run_at,
                next_run_at=summary.next_run_at,
                runs=runs,
                limit=page.limit,
                offset=page.offset,
                has_more=has_more,
            )
        except (TypeError, ValueError, OverflowError, OSError, ValidationError) as exc:
            raise ReadDataUnavailable("data_invalid") from exc

    def _cron_job_summary(self, record: object) -> CronJobSummary:
        """将公开 Cron 定义转换为不包含能力和投递信息的安全摘要。"""
        if not isinstance(record, dict):
            raise ValueError("record is invalid")
        schedule_type = self._required_text(record, "schedule_type")
        schedule_expr = self._required_text(record, "schedule_expr")
        prompt = self._required_text(record, "prompt")
        name = self._required_text(record, "name")
        timezone = self._optional_text(record.get("timezone"))
        paused = record.get("paused")
        if not isinstance(paused, bool):
            raise ValueError("paused is invalid")
        return CronJobSummary(
            job_id=self._required_text(record, "job_id"),
            name=self._redactor.preview_text(name),
            prompt_preview=self._redactor.preview_text(prompt),
            schedule=self._redactor.structure_text(
                f"{schedule_type}:{schedule_expr}",
                limit=self._redactor.limits.preview_text_limit,
            ),
            timezone=(
                self._redactor.structure_text(
                    timezone,
                    limit=self._redactor.limits.preview_text_limit,
                )
                if timezone is not None
                else None
            ),
            enabled=not paused and record.get("deleted_at") is None,
            last_run_at=self._timestamp(record.get("last_run_at")),
            next_run_at=self._timestamp(record.get("next_run_at")),
        )

    def _cron_run_summary(self, record: object) -> CronRunSummary:
        """仅映射适合管理页面展示的 Cron 运行历史字段。"""
        if not isinstance(record, dict):
            raise ValueError("record is invalid")
        result_summary = self._optional_text(record.get("result_summary"))
        return CronRunSummary(
            run_id=self._required_text(record, "run_id"),
            status=self._redactor.structure_text(
                self._required_text(record, "status"),
                limit=self._redactor.limits.preview_text_limit,
            ),
            scheduled_for=self._timestamp(record.get("scheduled_for")),
            started_at=self._timestamp(record.get("started_at")),
            finished_at=self._timestamp(record.get("finished_at")),
            result_summary=(
                self._redactor.error_text(result_summary)
                if result_summary is not None
                else None
            ),
        )


class CatalogReadService(_ReadServiceBase):
    """读取无需注册工具的当前 Skill 目录元数据。"""

    def __init__(
        self,
        context: DashboardReadContext | str | Path | None = None,
        redactor: DashboardRedactor | None = None,
        *,
        skills_dir: Path | str | None = None,
        capabilities: tuple[CapabilityDescriptor, ...] | None = None,
        toolsets: tuple[ToolsetDescriptor, ...] | None = None,
    ):
        super().__init__(context, redactor)
        self._skills_dir = (
            Path(skills_dir) if skills_dir is not None else hermes_home() / "skills"
        )
        self._capabilities = self._capability_snapshot(capabilities)
        self._toolsets = self._toolset_snapshot(toolsets)

    def list_skills(self, *, page: PageParams) -> SkillListResponse:
        """有限读取 Skill frontmatter，不导入工具适配层或触发注册。"""
        try:
            if not self._skills_dir.exists():
                return SkillListResponse(
                    items=[],
                    limit=page.limit,
                    offset=page.offset,
                    has_more=False,
                )
            if not self._skills_dir.is_dir():
                raise ReadDataUnavailable("catalog_unavailable")
            entries = sorted(
                (
                    entry
                    for entry in self._skills_dir.iterdir()
                    if not entry.is_symlink()
                    and entry.is_dir()
                    and _SKILL_NAME_RE.fullmatch(entry.name)
                ),
                key=lambda entry: (entry.name.lower(), entry.name),
            )
            page_items: list[SkillSummary] = []
            skipped = 0
            for entry in entries:
                item = self._skill_summary(entry)
                if item is None:
                    continue
                if skipped < page.offset:
                    skipped += 1
                    continue
                page_items.append(item)
                if len(page_items) >= page.fetch_limit:
                    break
            page_items, has_more = split_page(page_items, page)
            return SkillListResponse(
                items=page_items,
                limit=page.limit,
                offset=page.offset,
                has_more=has_more,
            )
        except ReadDataUnavailable:
            raise
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
            raise ReadDataUnavailable("catalog_unavailable") from exc

    def list_toolsets(self, *, page: PageParams) -> ToolsetListResponse:
        """读取应用装配期注入的不可变能力快照，不重复导入或注册工具。"""
        if self._capabilities is None or self._toolsets is None:
            raise ReadDataUnavailable("catalog_unavailable")
        try:
            tools_by_toolset: dict[str, list[CapabilityDescriptor]] = {}
            for capability in self._capabilities:
                tools_by_toolset.setdefault(capability.toolset, []).append(
                    capability
                )
            all_items = [
                self._toolset_summary(
                    descriptor,
                    tools_by_toolset.get(descriptor.name, ()),
                )
                for descriptor in self._toolsets
            ]
            all_items.sort(key=lambda item: item.name)
            visible_items = all_items[
                page.offset:page.offset + page.fetch_limit
            ]
            items, has_more = split_page(visible_items, page)
            return ToolsetListResponse(
                items=items,
                limit=page.limit,
                offset=page.offset,
                has_more=has_more,
                tool_details_available=True,
            )
        except (TypeError, ValueError, OverflowError, ValidationError) as exc:
            raise ReadDataUnavailable("catalog_unavailable") from exc

    @staticmethod
    def _capability_snapshot(
        capabilities: tuple[CapabilityDescriptor, ...] | None,
    ) -> tuple[CapabilityDescriptor, ...] | None:
        """只接受已冻结的能力元数据，避免服务持有可执行注册表。"""
        if capabilities is None:
            return None
        if not isinstance(capabilities, tuple) or not all(
            isinstance(item, CapabilityDescriptor)
            for item in capabilities
        ):
            raise TypeError("capabilities must be a tuple of CapabilityDescriptor")
        return capabilities

    @staticmethod
    def _toolset_snapshot(
        toolsets: tuple[ToolsetDescriptor, ...] | None,
    ) -> tuple[ToolsetDescriptor, ...] | None:
        """只接受已冻结的工具集聚合，保持启动期快照语义。"""
        if toolsets is None:
            return None
        if not isinstance(toolsets, tuple) or not all(
            isinstance(item, ToolsetDescriptor)
            for item in toolsets
        ):
            raise TypeError("toolsets must be a tuple of ToolsetDescriptor")
        return toolsets

    def _toolset_summary(
        self,
        descriptor: ToolsetDescriptor,
        capabilities: (
            list[CapabilityDescriptor]
            | tuple[CapabilityDescriptor, ...]
        ),
    ) -> ToolsetSummary:
        """将通用能力契约按现有 Dashboard 输出策略投影为 API 模型。"""
        tools = [
            self._tool_capability_summary(capability)
            for capability in sorted(capabilities, key=lambda item: item.name)
        ]
        return ToolsetSummary(
            name=self._redactor.structure_text(
                descriptor.name,
                limit=self._redactor.limits.preview_text_limit,
            ),
            available=True,
            environments=[
                self._redactor.structure_text(
                    value,
                    limit=self._redactor.limits.preview_text_limit,
                )
                for value in descriptor.execution_environments
            ],
            tool_count=len(tools),
            default_environments=[
                self._redactor.structure_text(
                    value,
                    limit=self._redactor.limits.preview_text_limit,
                )
                for value in descriptor.default_enabled_environments
            ],
            tools=tools,
        )

    def _tool_capability_summary(
        self,
        capability: CapabilityDescriptor,
    ) -> ToolCapabilitySummary:
        """只输出能力目录需要的参数名称与声明属性，不回传完整 schema。"""
        structure_limit = self._redactor.limits.preview_text_limit
        return ToolCapabilitySummary(
            name=self._redactor.structure_text(
                capability.name,
                limit=structure_limit,
            ),
            description=(
                self._redactor.preview_text(capability.description)
                if capability.description
                else None
            ),
            parameter_names=[
                self._redactor.structure_text(name, limit=structure_limit)
                for name in capability.parameter_names
            ],
            required_parameters=[
                self._redactor.structure_text(name, limit=structure_limit)
                for name in capability.required_parameters
            ],
            environments=[
                self._redactor.structure_text(value, limit=structure_limit)
                for value in capability.execution_environments
            ],
            default_environments=[
                self._redactor.structure_text(value, limit=structure_limit)
                for value in capability.default_enabled_environments
            ],
            unattended_allowed=capability.unattended_allowed,
            approval_mode=self._redactor.structure_text(
                capability.approval_mode,
                limit=structure_limit,
            ),
            risk_level=self._redactor.structure_text(
                capability.risk_level,
                limit=structure_limit,
            ),
            retry_safe=capability.retry_safe,
            unknown_on_crash=capability.unknown_on_crash,
            supports_cancellation=capability.supports_cancellation,
            has_status_check=capability.has_status_check,
        )

    def _skill_summary(self, skill_dir: Path) -> SkillSummary | None:
        """只解析受限前置元数据；单个损坏 Skill 不泄漏失败细节。"""
        skill_file = skill_dir / "SKILL.md"
        try:
            if not skill_file.is_file() or skill_file.is_symlink():
                return None
            metadata = _read_skill_frontmatter(skill_file)
            if metadata is None:
                return SkillSummary(name=skill_dir.name, available=False)
            name = metadata.get("name", skill_dir.name)
            description = metadata.get("description")
            skill_version = metadata.get("version")
            if not isinstance(name, str) or not name:
                return SkillSummary(name=skill_dir.name, available=False)
            if description is not None and not isinstance(description, str):
                return SkillSummary(name=skill_dir.name, available=False)
            if skill_version is not None and not isinstance(skill_version, str):
                return SkillSummary(name=skill_dir.name, available=False)
            return SkillSummary(
                name=self._redactor.structure_text(
                    name,
                    limit=self._redactor.limits.preview_text_limit,
                ),
                description=(
                    self._redactor.preview_text(description)
                    if description
                    else None
                ),
                version=(
                    self._redactor.structure_text(
                        skill_version,
                        limit=self._redactor.limits.preview_text_limit,
                    )
                    if skill_version
                    else None
                ),
                available=True,
            )
        except (OSError, UnicodeError, ValueError, yaml.YAMLError):
            return SkillSummary(name=skill_dir.name, available=False)


class ReadService:
    """兼容旧调用方的门面；业务读取均委托给领域服务。"""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        context: DashboardReadContext | None = None,
        redactor: DashboardRedactor | None = None,
        health_read_service: HealthReadService | None = None,
        session_read_service: SessionReadService | None = None,
        cron_read_service: CronReadService | None = None,
        catalog_read_service: CatalogReadService | None = None,
    ):
        shared_context = context or DashboardReadContext(db_path)
        shared_redactor = redactor or DashboardRedactor()
        self.health = health_read_service or HealthReadService(
            shared_context,
            shared_redactor,
        )
        self.sessions = session_read_service or SessionReadService(
            shared_context,
            shared_redactor,
        )
        self.cron = cron_read_service or CronReadService(
            shared_context,
            shared_redactor,
        )
        self.catalog = catalog_read_service or CatalogReadService(
            shared_context,
            shared_redactor,
        )

    def get_status(self) -> StatusResponse:
        """兼容既有状态读取调用。"""
        return self.health.get_status()

    def list_sessions(
        self,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> SessionListResponse:
        """兼容既有会话列表读取调用。"""
        return self.sessions.list_sessions(page=PageParams(limit=limit, offset=offset))

    def get_session(self, conversation_id: str) -> SessionDetailResponse:
        """兼容既有会话详情读取调用。"""
        return self.sessions.get_session(conversation_id)

    def list_cron_jobs(
        self,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> CronJobListResponse:
        """兼容既有 Cron 列表读取调用。"""
        return self.cron.list_cron_jobs(page=PageParams(limit=limit, offset=offset))

    def get_cron_job(
        self,
        job_id: str,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> CronJobDetailResponse:
        """兼容既有 Cron 详情读取调用。"""
        return self.cron.get_cron_job(
            job_id,
            page=PageParams(limit=limit, offset=offset),
        )

    def list_skills(
        self,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> SkillListResponse:
        """兼容既有 Skill 目录读取调用。"""
        return self.catalog.list_skills(page=PageParams(limit=limit, offset=offset))

    def list_toolsets(
        self,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> ToolsetListResponse:
        """兼容既有 Toolset 目录读取调用。"""
        return self.catalog.list_toolsets(
            page=PageParams(limit=limit, offset=offset),
        )


def _read_skill_frontmatter(skill_file: Path) -> dict[str, object] | None:
    """读取固定大小的 Skill frontmatter，避免目录枚举变成完整正文读取。"""
    with skill_file.open("r", encoding="utf-8") as handle:
        text = handle.read(_MAX_SKILL_FRONTMATTER_CHARS)
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}
    frontmatter: list[str] = []
    for line in lines[1:]:
        if line.strip() == "---":
            value = yaml.safe_load("".join(frontmatter)) or {}
            return value if isinstance(value, dict) else None
        frontmatter.append(line)
    return {}


__all__ = [
    "CatalogReadService",
    "CronReadService",
    "DashboardReadError",
    "HealthReadService",
    "ReadDataUnavailable",
    "ReadService",
    "ResourceNotFound",
    "SessionReadService",
]
